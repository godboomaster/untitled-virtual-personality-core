"""
Лёгкий HTTP-сервер экспорта данных памяти из ChromaDB.
Работает в фоновом потоке основного приложения на отдельном порту.

Доступ — только по токену: ?token=... или заголовок "Authorization: Bearer ...".
Токен — EXPORT_TOKEN, а без него — случайный, созданный при первом запуске
в <data>/export_token. Биндинг: EXPORT_HOST (по умолчанию 127.0.0.1).
"""

import hmac
import json
import os
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import chromadb
from app.core.config import Config, get_db_paths
from app.core.paths import data_dir
from app.core.persona import PersonaLayer
import logging

logger = logging.getLogger(__name__)

# Задаётся в start_export_server; пустой — сервер не запущен, доступа нет
EXPORT_TOKEN = ""


def _token_file() -> str:
    return str(data_dir() / "export_token")


def _load_token() -> str:
    # Без токена экспорт отдавал память всех персон любому локальному процессу
    # и любой открытой в браузере странице (ответ шёл с CORS *), поэтому
    # открытого режима нет: нет EXPORT_TOKEN — свой токен в файле (права 0600)
    env = os.getenv("EXPORT_TOKEN", "").strip()
    if env:
        return env
    path = _token_file()
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def _build_db_sources() -> dict:
    # Источники экспорта: STM/LTM/файлы для каждой установленной персоны
    # плюс служебный контекст default (у него своя база, но это не персона)
    contexts = sorted(set(PersonaLayer().available_personas()) | {"default"})
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
            return False
        expected = EXPORT_TOKEN.encode("utf-8")
        given = [parse_qs(parsed.query).get("token", [""])[0]]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            given.append(auth[len("Bearer "):])
        # compare_digest — без раннего выхода на первом несовпавшем символе
        return any(hmac.compare_digest(g.encode("utf-8"), expected) for g in given if g)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/api/export-memory", "/api/export-memory/list") and not self._authorized(parsed):
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Unauthorized: задайте ?token= или Authorization: Bearer "
                             "(токен — EXPORT_TOKEN или файл export_token в папке данных)".encode("utf-8"))
            return

        if parsed.path == "/api/export-memory":
            params = parse_qs(parsed.query)
            requested = params.get("db", [None])[0]

            if requested and requested in DB_SOURCES:
                source = DB_SOURCES[requested]
                data = {requested: dump_collection(source["path"], source["collection"])}
            else:
                data = {}
                for key, source in DB_SOURCES.items():
                    data[key] = dump_collection(source["path"], source["collection"])

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

        elif parsed.path == "/api/export-memory/list":
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
    # По умолчанию слушаем только localhost; EXPORT_HOST=0.0.0.0 открывает наружу.
    global EXPORT_TOKEN
    bind_host = host or os.getenv("EXPORT_HOST", "127.0.0.1")
    EXPORT_TOKEN = _load_token()
    token_src = "EXPORT_TOKEN" if os.getenv("EXPORT_TOKEN", "").strip() else _token_file()

    def _run():
        server = HTTPServer((bind_host, port), ExportHandler)
        logger.info(f"[ExportServer] Started on {bind_host}:{port} (токен: {token_src})")
        server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True, name="export-server")
    thread.start()
    return thread