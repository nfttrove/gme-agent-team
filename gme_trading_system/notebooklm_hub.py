"""
NotebookLM integration hub for historical pattern storage.

NOTE: Google NotebookLM has no official public API as of 2025.
This module provides a file-based stub that stores analysis transcripts locally
and queries them using ChromaDB for semantic search.
Replace with the official API when Google releases it.
"""
import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_memory.db")
CACHE_DIR = Path(os.path.dirname(__file__)) / "notebooklm_cache"


class TradingResearchHub:
    def __init__(self):
        CACHE_DIR.mkdir(exist_ok=True)
        self._init_vector_store()

    def _init_vector_store(self):
        try:
            import chromadb
            self.chroma = chromadb.PersistentClient(path=str(CACHE_DIR / "chroma"))
            self.collection = self.chroma.get_or_create_collection("trading_memory")
            self._use_chroma = True
        except Exception:
            self._use_chroma = False
            print("[notebooklm_hub] ChromaDB unavailable — using flat-file fallback")

    def ingest(self, content: str, metadata: dict | None = None) -> str:
        """Store an analysis transcript for future retrieval."""
        doc_id = f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        meta = metadata or {}
        meta["timestamp"] = datetime.now().isoformat()

        # Always save to flat file
        doc_path = CACHE_DIR / f"{doc_id}.json"
        doc_path.write_text(json.dumps({"id": doc_id, "content": content, "meta": meta}))

        if self._use_chroma:
            try:
                self.collection.add(
                    documents=[content],
                    metadatas=[meta],
                    ids=[doc_id],
                )
            except Exception as e:
                print(f"[notebooklm_hub] ChromaDB ingest error: {e}")

        return doc_id

    def query(self, question: str, n_results: int = 5) -> list[dict]:
        """Find historical analyses most relevant to the question."""
        if self._use_chroma:
            try:
                results = self.collection.query(query_texts=[question], n_results=n_results)
                return [
                    {"content": doc, "meta": meta}
                    for doc, meta in zip(results["documents"][0], results["metadatas"][0])
                ]
            except Exception as e:
                print(f"[notebooklm_hub] ChromaDB query error: {e}")

        # Flat-file fallback: return most recent N docs
        docs = sorted(CACHE_DIR.glob("doc_*.json"), reverse=True)[:n_results]
        return [json.loads(p.read_text()) for p in docs]

    def ingest_from_db(self, days: int = 7):
        """Pull recent agent_logs from SQLite and ingest into the hub."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM agent_logs ORDER BY timestamp DESC LIMIT ?",
            (days * 10,),
        )
        for row in cur.fetchall():
            self.ingest(
                content=row["content"],
                metadata={"agent": row["agent_name"], "task_type": row["task_type"]},
            )
        conn.close()
        print(f"[notebooklm_hub] Ingested recent agent logs")


hub = TradingResearchHub()
