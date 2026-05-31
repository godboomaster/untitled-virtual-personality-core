#!/usr/bin/env python3
"""
Миграция: загрузить STM из virt-p экспорта в ChromaDB virtual-persona-core.
Останавливает бота перед запуском!

Usage:
    cd /Users/ghost/Documents/virtual-persona-core
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 migrate_stm.py
"""

import json
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

DB_PATH = "data/connor/stm"
IMPORT_FILE = "/tmp/vp_stm_import.json"

# 1. Connect
client = chromadb.PersistentClient(path=DB_PATH)
embedder = SentenceTransformerEmbeddingFunction(
    model_name="paraphrase-multilingual-MiniLM-L12-v2"
)
collection = client.get_or_create_collection(
    "short_term_memory",
    embedding_function=embedder
)

existing = collection.count()
print(f"Existing in ChromaDB: {existing}")

# 2. Clear
if existing > 0:
    all_ids = collection.get()["ids"]
    collection.delete(ids=all_ids)
    print(f"Deleted {len(all_ids)} existing documents")

# 3. Load import data
with open(IMPORT_FILE) as f:
    messages = json.load(f)
print(f"Importing {len(messages)} messages")

# 4. Batch insert
batch_size = 100
for i in range(0, len(messages), batch_size):
    batch = messages[i:i + batch_size]
    ids = [m["id"] for m in batch]
    documents = [m["document"] for m in batch]
    metadatas = []
    for m in batch:
        meta = m.get("metadata", {})
        # Normalize: ensure required fields
        out = {
            "role": meta.get("role", "user"),
            "timestamp": meta.get("timestamp", 0),
            "chat_id": meta.get("chat_id") or meta.get("user_id", "default"),
        }
        if meta.get("user_name"):
            out["user_name"] = meta["user_name"]
        if meta.get("sender_id"):
            out["sender_id"] = meta["sender_id"]
        metadatas.append(out)

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(f"  Batch {i // batch_size + 1}: {len(batch)} inserted")

# 5. Verify
final = collection.count()
print(f"\nFinal ChromaDB count: {final}")

# Test vector search
test = collection.query(
    query_texts=["привет"],
    n_results=3,
    where={"chat_id": "-1003207877920"}
)
print(f"\nVerification: vector search 'привет' -> {len(test['documents'][0])} results")
for doc in test["documents"][0]:
    print(f"  - {doc[:80]}...")

print("\nDone. Deque will load from DB on bot startup.")
