import chromadb
from chromadb.config import Settings
# Eski Hatalı Satır:
# Yeni Doğru Satır:
from langchain_text_splitters import RecursiveCharacterTextSplitter
import os
from typing import List


def get_chroma_client():
    """Initialize and return a persistent ChromaDB client."""
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/chroma_db'))
    os.makedirs(db_path, exist_ok=True)
    client = chromadb.PersistentClient(
        path=db_path,
        settings=Settings(anonymized_telemetry=False)
    )
    return client


def get_or_create_collection(client, name: str = "rationai_docs"):
    """Get or create a ChromaDB collection with cosine similarity."""
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"}
    )


def ingest_documents(docs_path: str = None):
    """
    Ingest documents from the specified directory, split them into chunks,
    and store them in the ChromaDB collection 'rationai_docs'.

    Args:
        docs_path: Path to the directory containing documents to ingest.
                   Defaults to ../data/docs/ relative to this file.
    """
    if docs_path is None:
        docs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../data/docs'))

    if not os.path.exists(docs_path):
        print(f"Warning: Documents path '{docs_path}' does not exist.")
        return

    # Initialize ChromaDB client and collection
    client = get_chroma_client()
    collection = get_or_create_collection(client, "rationai_docs")

    # Initialize text splitter
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len,
    )

    # Collect all text files
    documents = []
    metadatas = []
    ids = []
    id_counter = 0

    for root, _, files in os.walk(docs_path):
        for file in files:
            if file.lower().endswith(('.txt', '.md')):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        text = f.read()
                except Exception as e:
                    print(f"Warning: Could not read file {file_path}: {e}")
                    continue

                # Split text into chunks
                chunks = text_splitter.split_text(text)
                for i, chunk in enumerate(chunks):
                    documents.append(chunk)
                    metadatas.append({"source": file, "chunk": i})
                    ids.append(f"{file}_{id_counter}_{i}")
                id_counter += 1

    if not documents:
        print(f"No text documents found in '{docs_path}'.")
        return {"documents_processed": 0, "chunks_created": 0}

    # Add to ChromaDB collection
    collection.add(
        documents=documents,
        metadatas=metadatas,
        ids=ids
    )
    print(f"Ingested {len(documents)} chunks from {id_counter} documents into collection 'rationai_docs'.")
    return {"documents_processed": id_counter, "chunks_created": len(documents)}


def query_documents(query_text: str, n_results: int = 3) -> List[str]:
    """
    Query the ChromaDB collection for semantically relevant chunks.

    Args:
        query_text: The query text to search for.
        n_results: Number of results to return (default: 3).

    Returns:
        List of relevant text chunks.
    """
    client = get_chroma_client()
    collection = get_or_create_collection(client, "rationai_docs")

    if collection.count() == 0:
        print("Warning: Collection is empty. Please ingest documents first.")
        return []

    results = collection.query(
        query_texts=[query_text],
        n_results=n_results,
        include=["documents"]  # We only need the documents
    )

    # Extract the list of documents for the first query
    documents = results.get('documents', [[]])[0]
    return documents


if __name__ == "__main__":
    result = ingest_documents()
    print(f"Processed {result['documents_processed']} documents, created {result['chunks_created']} chunks")