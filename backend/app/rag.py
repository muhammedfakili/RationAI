"""ChromaDB knowledge base over the Markdown/text files in data/docs/ (+ the user's notes).

CLI: `python -m backend.app.rag` rebuilds the index; `--if-empty` only builds it when
there is none yet (quickstart uses this so startup does not re-embed every time).
"""

import os
import re
import sys
from typing import Dict, List, Tuple

import chromadb
from chromadb.config import Settings
from langchain_text_splitters import RecursiveCharacterTextSplitter

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHROMA_PATH = os.path.join(PROJECT_ROOT, "data", "chroma_db")
DOCS_PATH = os.path.join(PROJECT_ROOT, "data", "docs")
# Kullanıcının zootekni notları da bilgi tabanına girer (fotoğraflar değil, döküm).
EXTRA_FILES = (os.path.join(PROJECT_ROOT, "data", "notlar", "rasyon_notlari.md"),)
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
    ) + [path for path in EXTRA_FILES if os.path.exists(path)]
    for path in files:
        with open(path, encoding="utf-8") as handle:
            chunks = splitter.split_text(handle.read())
        source = os.path.basename(path)
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
    return [text for text, _ in query_with_sources(query_text, n_results)]


# Türkçe kelime eşleşmesi: ChromaDB'nin varsayılan gömme modeli (all-MiniLM-L6-v2) İngilizce
# eğitilmiş; "kuruda yonca neden verilmez?" sorusuna içindekiler sayfası getiriyordu. Kelimeler
# küçük harfe çevrilip ilk 5 harfiyle (kaba bir kök: "yoncanın" → "yonca") karşılaştırılır,
# skor = eşleşen farklı kök sayısı. Sonuçlar önce kelime eşleşmesi, sonra anlam araması.
_STOPWORDS = frozenset("ve veya ile için neden niye nasıl ne bir bu şu da de mi mı mu mü ki "
                       "çok daha en gibi olarak olan olur olursa ama fakat hangi kaç".split())
_STEM = 5


def _stems(text: str) -> set:
    words = re.findall(r"[a-zçğıöşü]+", text.replace("I", "ı").replace("İ", "i").lower())
    return {w[:_STEM] for w in words if len(w) >= 3 and w not in _STOPWORDS}


_chunk_cache: Dict[int, List[Tuple[str, str, set]]] = {}


def _all_chunks(collection) -> List[Tuple[str, str, set]]:
    """Bütün parçalar (metin, kaynak, kökler); koleksiyon değişmedikçe bellekte tutulur."""
    count = collection.count()
    if count not in _chunk_cache:
        _chunk_cache.clear()
        data = collection.get(include=["documents", "metadatas"])
        _chunk_cache[count] = [(doc, (meta or {}).get("source", "?"), _stems(doc))
                               for doc, meta in zip(data["documents"], data["metadatas"])]
    return _chunk_cache[count]


def query_with_sources(query_text: str, n_results: int = 4) -> List[Tuple[str, str]]:
    """(parça, kaynak dosya adı) çiftleri — sohbet cevabı kaynağını gösterebilsin."""
    collection = get_or_create_collection(get_chroma_client())
    if collection.count() == 0:
        return []
    query_stems = _stems(query_text)
    scored = sorted(((len(query_stems & stems), doc, src) for doc, src, stems in _all_chunks(collection)),
                    key=lambda item: -item[0])
    hits: List[Tuple[str, str]] = [(doc, src) for score, doc, src in scored[:n_results]
                                   if score >= max(2, len(query_stems) // 3)]
    results = collection.query(query_texts=[query_text], n_results=n_results,
                               include=["documents", "metadatas"])
    for doc, meta in zip(results.get("documents", [[]])[0], results.get("metadatas", [[]])[0]):
        if len(hits) >= n_results:
            break
        if all(doc != seen for seen, _ in hits):
            hits.append((doc, (meta or {}).get("source", "?")))
    return hits[:n_results]


if __name__ == "__main__":
    if "--if-empty" in sys.argv and get_or_create_collection(get_chroma_client()).count() > 0:
        print("Knowledge base already indexed, skipping (run without --if-empty to rebuild).")
    else:
        result = ingest_documents()
        print(f"Processed {result['documents_processed']} documents, created {result['chunks_created']} chunks")
