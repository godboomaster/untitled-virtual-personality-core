"""
Lightweight HTTP server for exporting all ChromaDB memory data.
Runs alongside the main app on a separate port.

Доступ: если задан EXPORT_TOKEN — требуется ?token=... или заголовок
"Authorization: Bearer ...". Биндинг: EXPORT_HOST (по умолчанию 127.0.0.1).
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import chromadb
from app.core.config import Config, get_db_paths
import logging

logger = logging.getLogger(__name__)

EXPORT_TOKEN = os.getenv("EXPORT_TOKEN", "")


def _build_db_sources() -> dict:
   # Строит маппинг динамически на основе persona-папок
    contexts = ["connor", "arrodes", "verso", "assistant", "default"]
    sources = {}
    for ctx in contexts:
        paths = get_db_paths(ctx)
        label_prefix = ctx.capitalize()
        sources[f"{ctx}_stm"] = {"path": paths["stm"], "collection": "short_term_memory", "label": f"{label_prefix} STM"}
        sources[f"{ctx}_ltm"] = {"path": paths["ltm"], "collection": "long_term_memory", "label": f"{label_prefix} LTM"}
        sources[f"{ctx}_files"] = {"path": paths["files"], "collection": "file_documents", "label": f"{label_prefix} Files"}
    return sources

DB_SOURCES = _build_db_sources()


def dump_collection(db_path: str, collection_name: str) -> dict:
    # Dump all data from a ChromaDB collection.
    # Эмбеддер не нужен для чтения; несуществующие БД не создаём на диске.
    if not os.path.exists(db_path):
        return {"count": 0, "documents": []}
    try:
        client = chromadb.PersistentClient(path=db_path)
        collection = client.get_collection(collection_name)

        if collection.count() == 0:
            return {"count": 0, "documents": []}

        results = collection.get(include=["documents", "metadatas"])

        documents = []
        for i, doc in enumerate(results["documents"]):
            metadata = results["metadatas"][i] if results["metadatas"] else {}
            documents.append({
                "id": results["ids"][i],
                "document": doc,
                "metadata": metadata,
            })

        return {"count": len(documents), "documents": documents}

    except Exception as e:
        return {"error": str(e), "count": 0, "documents": []}


class ExportHandler(BaseHTTPRequestHandler):
    def _authorized(self, parsed) -> bool:
        if not EXPORT_TOKEN:
            return True
        params = parse_qs(parsed.query)
        if params.get("token", [None])[0] == EXPORT_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {EXPORT_TOKEN}"

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/api/export-memory", "/api/export-memory/list") and not self._authorized(parsed):
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Unauthorized: задайте ?token= или Authorization: Bearer".encode("utf-8"))
            return

        if parsed.path == "/api/export-memory":
            params = parse_qs(parsed.query)
            requested = params.get("db", [None])[0]

            if requested and requested in DB_SOURCES:
                # Export single database
                source = DB_SOURCES[requested]
                data = {requested: dump_collection(source["path"], source["collection"])}
            else:
                # Export all databases
                data = {}
                for key, source in DB_SOURCES.items():
                    data[key] = dump_collection(source["path"], source["collection"])

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

        elif parsed.path == "/api/export-memory/list":
            # List available databases
            listing = {}
            for key, source in DB_SOURCES.items():
                if not os.path.exists(source["path"]):
                    listing[key] = {"label": source["label"], "count": 0}
                    continue
                try:
                    client = chromadb.PersistentClient(path=source["path"])
                    col = client.get_collection(source["collection"])
                    listing[key] = {"label": source["label"], "count": col.count()}
                except Exception as e:
                    listing[key] = {"label": source["label"], "error": str(e)}

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(listing, ensure_ascii=False, indent=2).encode("utf-8"))

        elif parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def log_message(self, format, *args):
        logger.info(f"[ExportServer] {format % args}")


def start_export_server(port: int = 8080, host: str = None):
    # Start the export server in a background thread.
    # По умолчанию слушаем только localhost; EXPORT_HOST=0.0.0.0 открывает наружу.
    bind_host = host or os.getenv("EXPORT_HOST", "127.0.0.1")

    def _run():
        server = HTTPServer((bind_host, port), ExportHandler)
        logger.info(f"[ExportServer] Started on {bind_host}:{port} (token: {'on' if EXPORT_TOKEN else 'off'})")
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True, name="export-server")
    thread.start()
    return thread