"""Optional local RAG index builder."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeAlias

MetadataValue: TypeAlias = str | int | float | bool | None
ChunkMetadata: TypeAlias = dict[str, MetadataValue]
LOCAL_RAG_IDENTITY_SCHEMA_VERSION = "local-rag-identity-v1"
DEFAULT_LOCAL_RAG_RETAIN_RECENT_IDENTITIES = 2


def _is_reparse_path(path: Path) -> bool:
    """Treat links and Windows reparse points as untrusted sidecars."""

    try:
        path_stat = os.lstat(os.fspath(path))
    except OSError:
        return True
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    return bool(
        getattr(path_stat, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


class LocalRAGIndex:
    """Best-effort Chroma index over preprocess chunks."""

    def __init__(self, persist_dir: str, logger: Any = None):
        self.persist_dir = persist_dir
        self.logger = logger

    def is_available(self) -> bool:
        try:
            import chromadb  # type: ignore
            from sentence_transformers import SentenceTransformer  # type: ignore

            return bool(chromadb and SentenceTransformer)
        except Exception:  # noqa: BLE001 - optional dependency discovery.
            return False

    @staticmethod
    def _identity_key(identity: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _collection_name_for_identity(collection_name: str, identity_key: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", str(collection_name or "collection"))
        suffix = str(identity_key or "")[:16]
        return f"{base[:45]}-{suffix}"[:63].rstrip("-_")

    def _read_identity_sidecar(
        self,
        identity_path: Path,
        *,
        collection_name: str,
        candidate_name: str,
    ) -> tuple[str, dict[str, Any]] | None:
        if _is_reparse_path(identity_path):
            return None
        try:
            if not identity_path.is_file():
                return None
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                return None
            identity_key = payload["identity_key"]
            if (
                payload.get("schema_version") != LOCAL_RAG_IDENTITY_SCHEMA_VERSION
                or not isinstance(identity_key, str)
                or len(identity_key) != 64
                or any(character not in "0123456789abcdef" for character in identity_key)
                or not isinstance(payload.get("identity"), Mapping)
            ):
                return None
            if self._identity_key(payload["identity"]) != identity_key:
                return None
            if candidate_name != collection_name and candidate_name != self._collection_name_for_identity(
                collection_name,
                identity_key,
            ):
                return None
            return identity_key, dict(payload["identity"])
        except (
            KeyError,
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
            json.JSONDecodeError,
        ):
            return None

    def _identity_records(
        self,
        collection_name: str,
    ) -> list[tuple[float, str, Path]]:
        root = Path(self.persist_dir)
        if not root.is_dir() or _is_reparse_path(root):
            return []
        base_name = str(collection_name)
        base_filename = f"{base_name}.identity.json"
        variant_prefix = re.sub(
            r"[^A-Za-z0-9_-]+", "-", base_name or "collection"
        )[:45] + "-"
        records: list[tuple[float, str, Path]] = []
        for identity_path in root.iterdir():
            if identity_path.name == base_filename:
                candidate_name = base_name
            elif (
                identity_path.name.endswith(".identity.json")
                and identity_path.name.startswith(variant_prefix)
            ):
                candidate_name = identity_path.name[: -len(".identity.json")]
            else:
                continue
            if (
                self._read_identity_sidecar(
                    identity_path,
                    collection_name=base_name,
                    candidate_name=candidate_name,
                )
                is None
            ):
                continue
            try:
                records.append(
                    (float(identity_path.lstat().st_mtime_ns), candidate_name, identity_path)
                )
            except OSError:
                continue
        return records

    def _collection_metadata_matches(
        self,
        client: Any,
        *,
        collection_name: str,
        identity_key: str,
        identity: Mapping[str, Any],
    ) -> bool:
        get_collection = getattr(client, "get_collection", None)
        if get_collection is None:
            return True
        try:
            collection = get_collection(name=collection_name)
            metadata = getattr(collection, "metadata", None)
        except Exception as exc:  # noqa: BLE001 - fail closed on backend uncertainty.
            if self.logger:
                self.logger.warning(
                    f"Local RAG collection metadata lookup failed for {collection_name}: {exc}"
                )
            return False
        if metadata is None:
            return True
        if not isinstance(metadata, Mapping):
            return False
        if metadata.get("identity_key") != identity_key:
            return False
        metadata_identity: dict[str, Any] = {}
        for field, expected_value in identity.items():
            if field in metadata and metadata[field] != expected_value:
                return False
            if field in metadata:
                metadata_identity[field] = metadata[field]
        if len(metadata_identity) != len(identity):
            return True
        try:
            return self._identity_key(metadata_identity) == identity_key
        except (TypeError, ValueError, OverflowError, RecursionError):
            return False

    def _prune_stale_identity_collections(
        self,
        client: Any,
        *,
        collection_name: str,
        current_collection_name: str,
        retain_recent_identities: int,
    ) -> tuple[str, ...]:
        """Bound one source's immutable cache while preserving current identity."""

        records = self._identity_records(collection_name)
        current_name = str(current_collection_name)
        previous = sorted(
            (record for record in records if record[1] != current_name),
            key=lambda record: (record[0], record[1]),
            reverse=True,
        )
        keep_names = {current_name}
        keep_names.update(
            record[1]
            for record in previous[: max(0, int(retain_recent_identities))]
        )
        try:
            existing_names = {
                str(item if isinstance(item, str) else getattr(item, "name", ""))
                for item in client.list_collections()
            }
        except Exception as exc:  # noqa: BLE001 - backend versions vary.
            if self.logger:
                self.logger.warning(f"Local RAG retention inventory failed: {exc}")
            return ()

        removed: list[str] = []
        for _mtime, candidate_name, identity_path in sorted(
            records,
            key=lambda record: (record[0], record[1]),
        ):
            if candidate_name in keep_names:
                continue
            identity_record = self._read_identity_sidecar(
                identity_path,
                collection_name=str(collection_name),
                candidate_name=candidate_name,
            )
            if identity_record is None:
                continue
            identity_key, identity = identity_record
            try:
                if _is_reparse_path(identity_path):
                    continue
                if candidate_name in existing_names:
                    if not self._collection_metadata_matches(
                        client,
                        collection_name=candidate_name,
                        identity_key=identity_key,
                        identity=identity,
                    ):
                        continue
                    if _is_reparse_path(identity_path):
                        continue
                    client.delete_collection(name=candidate_name)
                if _is_reparse_path(identity_path):
                    continue
                identity_path.unlink()
            except (OSError, TypeError, ValueError) as exc:
                if self.logger:
                    self.logger.warning(
                        f"Local RAG retention could not remove {candidate_name}: {exc}"
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - preserve sidecar for retry.
                if self.logger:
                    self.logger.warning(
                        f"Local RAG collection deletion failed for {candidate_name}: {exc}"
                    )
                continue
            removed.append(candidate_name)
        return tuple(removed)

    def build_from_chunks(
        self,
        collection_name: str,
        chunks: list[dict[str, Any]],
        *,
        source_pdf_sha256: str = "",
        processing_fingerprint: str = "",
        chunk_schema_version: str = "chunks-v1",
        embedding_model: str = "all-MiniLM-L6-v2",
        allow_model_download: bool | None = None,
        retain_recent_identities: int = DEFAULT_LOCAL_RAG_RETAIN_RECENT_IDENTITIES,
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
        identity_key = self._identity_key(identity)
        os.makedirs(self.persist_dir, exist_ok=True)
        base_identity_path = os.path.join(self.persist_dir, f"{collection_name}.identity.json")
        selected_collection_name = str(collection_name)
        identity_path = base_identity_path
        if os.path.lexists(base_identity_path):
            existing = self._read_identity_sidecar(
                Path(base_identity_path),
                collection_name=str(collection_name),
                candidate_name=str(collection_name),
            )
            if existing is None or existing[0] != identity_key:
                if self.logger:
                    self.logger.warning(
                        "Local RAG identity changed; selecting a new immutable collection."
                    )
                selected_collection_name = self._collection_name_for_identity(
                    collection_name,
                    identity_key,
                )
                identity_path = os.path.join(
                    self.persist_dir,
                    f"{selected_collection_name}.identity.json",
                )
                if os.path.lexists(identity_path):
                    selected_existing = self._read_identity_sidecar(
                        Path(identity_path),
                        collection_name=str(collection_name),
                        candidate_name=selected_collection_name,
                    )
                    if selected_existing is None or selected_existing[0] != identity_key:
                        return False
        elif self._identity_records(collection_name):
            selected_collection_name = self._collection_name_for_identity(
                collection_name,
                identity_key,
            )
            identity_path = os.path.join(
                self.persist_dir,
                f"{selected_collection_name}.identity.json",
            )
            if os.path.lexists(identity_path):
                selected_existing = self._read_identity_sidecar(
                    Path(identity_path),
                    collection_name=str(collection_name),
                    candidate_name=selected_collection_name,
                )
                if selected_existing is None or selected_existing[0] != identity_key:
                    return False
        client = chromadb.PersistentClient(path=self.persist_dir)
        collection = client.get_or_create_collection(
            name=selected_collection_name,
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
        except Exception:  # noqa: BLE001 - optional model backend.
            return False
        texts = [str(chunk.get("text", "")) for chunk in chunks]
        embeddings = embedder.encode(texts).tolist()
        ids = [str(chunk.get("chunk_id", index)) for index, chunk in enumerate(chunks)]
        metadatas: list[Metadata] = []
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
                {
                    "schema_version": LOCAL_RAG_IDENTITY_SCHEMA_VERSION,
                    "identity_key": identity_key,
                    "identity": identity,
                },
                handle,
                ensure_ascii=False,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, identity_path)
        self._prune_stale_identity_collections(
            client,
            collection_name=collection_name,
            current_collection_name=selected_collection_name,
            retain_recent_identities=retain_recent_identities,
        )
        return True

    def build_from_file(self, collection_name: str, chunks_path: str, **kwargs: Any) -> bool:
        if not os.path.exists(chunks_path):
            return False
        with open(chunks_path, "r", encoding="utf-8") as handle:
            chunks = json.load(handle)
        if not isinstance(chunks, list):
            return False
        return self.build_from_chunks(collection_name, chunks, **kwargs)
