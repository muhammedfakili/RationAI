"""ChromaDB knowledge base over the Markdown/text files in data/docs/.

CLI: `python -m backend.app.rag` rebuilds the index; `--if-empty` only builds it when
there is none yet (quickstart uses this so startup does not re-embed every time).
"""

import os
import sys
from typing import Dict, List

import chromadb
from chromadb.config import Settings
from langchain_text_splitters import RecursiveCharacterTextSplitter

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHROMA_PATH = os.path.join(PROJECT_ROOT, "data", "chroma_db")
DOCS_PATH = os.path.join(PROJECT_ROOT, "data", "docs")
COLLECTION = "rationai_docs"


def get_chroma_client():
    """Return a persistent ChromaDB client."""
    os.makedirs(CHROMA_PATH, exist_ok=True)
    return chromadb.PersistentClient(path=CHROMA_PATH, settings=Settings(anonymized_telemetry=False))


def get_or_create_collection(client, name: str = COLLECTION):
    """Get or create the collection (cosine similarity)."""
    return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def ingest_documents(docs_path: str = DOCS_PATH) -> Dict[str, int]:
    """Rebuild the collection from every .md/.txt file under `docs_path`.

    The collection is dropped first, so removed or edited documents leave no stale chunks.
    """
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    documents: List[str] = []
    metadatas: List[dict] = []
    ids: List[str] = []
    files = sorted(
        os.path.join(root, name)
        for root, _, names in os.walk(docs_path)
        for name in names if name.lower().endswith((".txt", ".md"))
    )
    for path in files:
        with open(path, encoding="utf-8") as handle:
            chunks = splitter.split_text(handle.read())
        source = os.path.relpath(path, docs_path)
        for i, chunk in enumerate(chunks):
            documents.append(chunk)
            metadatas.append({"source": source, "chunk": i})
            ids.append(f"{source}_{i}")

    client = get_chroma_client()
    try:
        client.delete_collection(COLLECTION)
    except Exception:  # noqa: BLE001 - chromadb raises different errors for "not found"
        pass
    collection = get_or_create_collection(client)
    if documents:
        collection.add(documents=documents, metadatas=metadatas, ids=ids)
    return {"documents_processed": len(files), "chunks_created": len(documents)}


def query_documents(query_text: str, n_results: int = 3) -> List[str]:
    """Return the chunks most relevant to `query_text` (empty if nothing is indexed)."""
    collection = get_or_create_collection(get_chroma_client())
    if collection.count() == 0:
        return []
    results = collection.query(query_texts=[query_text], n_results=n_results, include=["documents"])
    return results.get("documents", [[]])[0]


if __name__ == "__main__":
    if "--if-empty" in sys.argv and get_or_create_collection(get_chroma_client()).count() > 0:
        print("Knowledge base already indexed, skipping (run without --if-empty to rebuild).")
    else:
        result = ingest_documents()
        print(f"Processed {result['documents_processed']} documents, created {result['chunks_created']} chunks")
