import json
from pathlib import Path

import pytest

from rag import local_rag
from rag.local_rag import LocalRAGIndex


COLLECTION_NAME = "source"


def _identity(seed: str) -> dict[str, str]:
    return {
        "source_pdf_sha256": seed * 64,
        "processing_fingerprint": "processing-fingerprint",
        "chunk_schema_version": "chunks-v1",
        "embedding_model": "all-MiniLM-L6-v2",
    }


def _write_sidecar(
    index: LocalRAGIndex,
    root: Path,
    identity: dict[str, str],
    *,
    identity_key: str | None = None,
    filename_key: str | None = None,
) -> tuple[str, Path]:
    actual_key = index._identity_key(identity)
    sidecar_key = identity_key or actual_key
    collection = index._collection_name_for_identity(
        COLLECTION_NAME,
        filename_key or sidecar_key,
    )
    path = root / f"{collection}.identity.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "local-rag-identity-v1",
                "identity_key": sidecar_key,
                "identity": identity,
            }
        ),
        encoding="utf-8",
    )
    return collection, path


class _Collection:
    def __init__(self, metadata: dict[str, object] | None) -> None:
        self.metadata = metadata


class _Client:
    def __init__(
        self,
        collections: dict[str, dict[str, object] | None],
        *,
        delete_error: Exception | None = None,
    ) -> None:
        self.collections = dict(collections)
        self.delete_error = delete_error
        self.deleted: list[str] = []

    def list_collections(self) -> list[str]:
        return sorted(self.collections)

    def get_collection(self, *, name: str) -> _Collection:
        return _Collection(self.collections[name])

    def delete_collection(self, *, name: str) -> None:
        self.deleted.append(name)
        if self.delete_error is not None:
            raise self.delete_error
        self.collections.pop(name)


def _matching_metadata(index: LocalRAGIndex, identity: dict[str, str]) -> dict[str, object]:
    return {
        "identity_key": index._identity_key(identity),
        **identity,
    }


def _prune(
    index: LocalRAGIndex,
    client: _Client,
) -> tuple[str, ...]:
    return index._prune_stale_identity_collections(
        client,
        collection_name=COLLECTION_NAME,
        current_collection_name="current-collection",
        retain_recent_identities=0,
    )


def test_modified_identity_body_with_unchanged_key_never_deletes_sidecar(
    tmp_path: Path,
) -> None:
    index = LocalRAGIndex(str(tmp_path))
    original = _identity("a")
    original_key = index._identity_key(original)
    modified = {**original, "processing_fingerprint": "modified"}
    collection, sidecar = _write_sidecar(
        index,
        tmp_path,
        modified,
        identity_key=original_key,
    )
    client = _Client({collection: None})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_file()


def test_forged_identity_key_never_authorizes_deletion(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    identity = _identity("b")
    forged_key = "f" * 64
    collection, sidecar = _write_sidecar(
        index,
        tmp_path,
        identity,
        identity_key=forged_key,
    )
    client = _Client({collection: None})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_file()


def test_filename_key_mismatch_never_authorizes_deletion(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    identity = _identity("c")
    identity_key = index._identity_key(identity)
    collection, sidecar = _write_sidecar(
        index,
        tmp_path,
        identity,
        filename_key="d" * 64,
    )
    client = _Client({collection: None})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_file()
    assert collection != index._collection_name_for_identity(COLLECTION_NAME, identity_key)


def test_collection_metadata_mismatch_never_authorizes_deletion(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    identity = _identity("e")
    collection, sidecar = _write_sidecar(index, tmp_path, identity)
    mismatched_metadata = _matching_metadata(index, identity)
    mismatched_metadata["source_pdf_sha256"] = "mismatched"
    client = _Client({collection: mismatched_metadata})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_file()


def test_backend_deletion_failure_preserves_sidecar(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    identity = _identity("g")
    collection, sidecar = _write_sidecar(index, tmp_path, identity)
    client = _Client(
        {collection: _matching_metadata(index, identity)},
        delete_error=RuntimeError("backend delete failed"),
    )

    assert _prune(index, client) == ()
    assert client.deleted == [collection]
    assert sidecar.is_file()


def test_current_identity_is_protected_from_pruning(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    current_identity = _identity("h")
    stale_identity = _identity("i")
    current_collection, current_sidecar = _write_sidecar(
        index,
        tmp_path,
        current_identity,
    )
    stale_collection, stale_sidecar = _write_sidecar(
        index,
        tmp_path,
        stale_identity,
    )
    client = _Client(
        {
            current_collection: _matching_metadata(index, current_identity),
            stale_collection: _matching_metadata(index, stale_identity),
        }
    )

    removed = index._prune_stale_identity_collections(
        client,
        collection_name=COLLECTION_NAME,
        current_collection_name=current_collection,
        retain_recent_identities=0,
    )

    assert removed == (stale_collection,)
    assert client.deleted == [stale_collection]
    assert current_sidecar.is_file()
    assert not stale_sidecar.exists()


def test_malformed_sidecar_never_authorizes_deletion(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    collection = index._collection_name_for_identity(COLLECTION_NAME, "a" * 64)
    sidecar = tmp_path / f"{collection}.identity.json"
    sidecar.write_text("{malformed", encoding="utf-8")
    client = _Client({collection: None})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_file()


@pytest.mark.optional
def test_symlink_sidecar_never_authorizes_deletion(tmp_path: Path) -> None:
    index = LocalRAGIndex(str(tmp_path))
    identity = _identity("k")
    collection = index._collection_name_for_identity(
        COLLECTION_NAME,
        index._identity_key(identity),
    )
    target = tmp_path / "outside.identity.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "local-rag-identity-v1",
                "identity_key": index._identity_key(identity),
                "identity": identity,
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / f"{collection}.identity.json"
    try:
        sidecar.symlink_to(target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable in this environment")
        raise
    client = _Client({collection: None})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_symlink()
    assert target.is_file()


def test_reparse_sidecar_never_authorizes_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = LocalRAGIndex(str(tmp_path))
    identity = _identity("l")
    collection, sidecar = _write_sidecar(index, tmp_path, identity)
    real_is_reparse_path = local_rag._is_reparse_path

    def mark_sidecar_as_reparse(path: Path) -> bool:
        return Path(path) == sidecar or real_is_reparse_path(path)

    monkeypatch.setattr(local_rag, "_is_reparse_path", mark_sidecar_as_reparse)
    client = _Client({collection: None})

    assert _prune(index, client) == ()
    assert client.deleted == []
    assert sidecar.is_file()
