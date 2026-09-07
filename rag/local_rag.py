"""Optional local RAG index builder."""

from __future__ import annotations

import json
import os
import hashlib
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

    def build_from_chunks(
        self,
        collection_name: str,
        chunks: List[Dict[str, Any]],
        *,
        source_pdf_sha256: str = "",
        processing_fingerprint: str = "",
        chunk_schema_version: str = "chunks-v1",
        embedding_model: str = "all-MiniLM-L6-v2",
        allow_model_download: bool | None = None,
    ) -> bool:
        if not chunks or not self.is_available():
            return False

        import chromadb  # type: ignore
        from chromadb.api.types import Metadata  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore

        allow_download = (
            str(os.getenv("AUTO_GENERATE_LOCAL_RAG_ALLOW_MODEL_DOWNLOAD", "0")).strip().casefold()
            in {"1", "true", "yes", "on"}
            if allow_model_download is None
            else bool(allow_model_download)
        )
        identity = {
            "source_pdf_sha256": str(source_pdf_sha256 or ""),
            "processing_fingerprint": str(processing_fingerprint or ""),
            "chunk_schema_version": str(chunk_schema_version or ""),
            "embedding_model": str(embedding_model or ""),
        }
        identity_key = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        os.makedirs(self.persist_dir, exist_ok=True)
        identity_path = os.path.join(self.persist_dir, f"{collection_name}.identity.json")
        if os.path.isfile(identity_path):
            try:
                with open(identity_path, "r", encoding="utf-8") as handle:
                    existing = json.load(handle)
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False
            if not isinstance(existing, dict) or existing.get("identity_key") != identity_key:
                if self.logger:
                    self.logger.warning(
                        "Local RAG identity does not match the requested source; refusing stale reuse."
                    )
                return False
        client = chromadb.PersistentClient(path=self.persist_dir)
        collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"identity_key": identity_key, **identity},
        )
        try:
            if allow_download:
                embedder = SentenceTransformer(embedding_model)
            else:
                embedder = SentenceTransformer(embedding_model, local_files_only=True)
        except TypeError:
            # Older sentence-transformers versions do not expose a local-only
            # constructor flag. Refuse to instantiate them because doing so
            # could silently download a model during an offline run.
            if self.logger:
                self.logger.warning(
                    "Local RAG embedding library lacks local_files_only; model download is blocked."
                )
            return False
        except Exception:
            return False
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
        temp_path = identity_path + f".{os.getpid()}.tmp"
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {"schema_version": "local-rag-identity-v1", "identity_key": identity_key, "identity": identity},
                handle,
                ensure_ascii=False,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, identity_path)
        return True

    def build_from_file(self, collection_name: str, chunks_path: str, **kwargs: Any) -> bool:
        if not os.path.exists(chunks_path):
            return False
        with open(chunks_path, "r", encoding="utf-8") as handle:
            chunks = json.load(handle)
        if not isinstance(chunks, list):
            return False
        return self.build_from_chunks(collection_name, chunks, **kwargs)
