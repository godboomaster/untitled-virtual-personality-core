#!/usr/bin/env python3
"""
STM Migration: Load last 500 messages into fresh ChromaDB.
Run AFTER stopping the bot.

Usage:
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 migrate_stm_500.py
"""

import json
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

DB_PATH = "data/connor/stm"

# 1. Connect to existing ChromaDB
client = chromadb.PersistentClient(path=DB_PATH)
embedder = SentenceTransformerEmbeddingFunction(
    model_name="paraphrase-multilingual-MiniLM-L12-v2"
)
collection = client.get_or_create_collection(
    "short_term_memory",
    embedding_function=embedder
)

existing = collection.count()
print(f"Existing documents in ChromaDB: {existing}")

# 2. Clear all existing data
if existing > 0:
    all_data = collection.get()
    all_ids = all_data["ids"]
    collection.delete(ids=all_ids)
    print(f"Deleted {len(all_ids)} documents")

# 3. Load the 500 messages
with open("/tmp/stm_import_500.json") as f:
    messages = json.load(f)
print(f"Loaded {len(messages)} messages to import")

# 4. Batch insert into ChromaDB
batch_size = 100
for i in range(0, len(messages), batch_size):
    batch = messages[i:i+batch_size]
    ids = [m["chroma_id"] for m in batch]
    documents = [m["document"] for m in batch]
    metadatas = []
    for m in batch:
        meta = {"role": m["role"], "timestamp": m["timestamp"], "chat_id": m["chat_id"]}
        if m.get("user_name"):
            meta["user_name"] = m["user_name"]
        if m.get("sender_id"):
            meta["sender_id"] = m["sender_id"]
        metadatas.append(meta)
    
    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(f"  Inserted batch {i//batch_size + 1}: {len(batch)} documents")

final_count = collection.count()
print(f"Final ChromaDB count: {final_count}")

# 5. Verify: test vector search
test_results = collection.query(
    query_texts=["привет"],
    n_results=3,
    where={"chat_id": "-1003207877920"}
)
print(f"\nVerification: vector search for 'привет' returned {len(test_results['documents'][0])} results")
for doc in test_results["documents"][0]:
    print(f"  - {doc[:80]}...")

print("\nDone! ChromaDB reloaded with last 500 messages.")
print("Deque (last 15 per chat) will be loaded automatically on bot startup via _load_from_db().")
