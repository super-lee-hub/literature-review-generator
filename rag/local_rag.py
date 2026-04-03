"""Optional local RAG index builder."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, TypeAlias


MetadataValue: TypeAlias = str | int | float | bool | None
ChunkMetadata: TypeAlias = Dict[str, MetadataValue]


class LocalRAGIndex:
    """Best-effort Chroma index over preprocess chunks."""

    def __init__(self, persist_dir: str, logger: Any = None):
        self.persist_dir = persist_dir
        self.logger = logger

    def is_available(self) -> bool:
        try:
            import chromadb  # type: ignore
            from sentence_transformers import SentenceTransformer  # type: ignore

            return True
        except Exception:
            return False

    def build_from_chunks(self, collection_name: str, chunks: List[Dict[str, Any]]) -> bool:
        if not chunks or not self.is_available():
            return False

        import chromadb  # type: ignore
        from chromadb.api.types import Metadata  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore

        client = chromadb.PersistentClient(path=self.persist_dir)
        collection = client.get_or_create_collection(name=collection_name)
        embedder = SentenceTransformer("all-MiniLM-L6-v2")
        texts = [str(chunk.get("text", "")) for chunk in chunks]
        embeddings = embedder.encode(texts).tolist()
        ids = [str(chunk.get("chunk_id", index)) for index, chunk in enumerate(chunks)]
        metadatas: List[Metadata] = []
        for chunk in chunks:
            page_number = chunk.get("page_number")
            page_number_value: MetadataValue
            if isinstance(page_number, (str, int, float, bool)) or page_number is None:
                page_number_value = page_number
            else:
                page_number_value = str(page_number)
            metadatas.append(
                {
                    "page_number": page_number_value,
                    "source": str(chunk.get("source", "page")),
                }
            )
        collection.upsert(ids=ids, documents=texts, embeddings=embeddings, metadatas=metadatas)
        return True

    def build_from_file(self, collection_name: str, chunks_path: str) -> bool:
        if not os.path.exists(chunks_path):
            return False
        with open(chunks_path, "r", encoding="utf-8") as handle:
            chunks = json.load(handle)
        if not isinstance(chunks, list):
            return False
        return self.build_from_chunks(collection_name, chunks)
