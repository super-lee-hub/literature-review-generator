from __future__ import annotations

import json
from pathlib import Path

from outline.adoption_transaction import (
    ADOPTION_POINTER_ARTIFACT_ID,
    OutlineAdoptionTransaction,
    current_adoption_record,
)
from tests.test_outline_v3_semantic_execution import _executor


def test_adoption_uses_versioned_identity_and_current_pointer(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.run()
    assert result.ok is True

    registry = executor.registry
    source = registry.get("outline-v3:final_outline")
    assert source is not None
    adopted = OutlineAdoptionTransaction(executor.workspace, registry).adopt(
        source_artifact_id=source.artifact_id,
        actor="test-researcher",
        reason="verify versioned adoption identity",
        expected_hash=source.content_hash,
    )

    assert adopted.status == "succeeded"
    assert adopted.adopted_artifact_id == f"outline-v3:adoption:{source.content_hash[:16]}"
    assert current_adoption_record(registry).artifact_id == adopted.adopted_artifact_id  # type: ignore[union-attr]

    pointer = registry.get(ADOPTION_POINTER_ARTIFACT_ID)
    assert pointer is not None and pointer.status == "ready"
    pointer_payload = json.loads(Path(pointer.path).read_text(encoding="utf-8"))
    assert pointer_payload["role"] == "current"
    assert pointer_payload["current_adoption_artifact_id"] == adopted.adopted_artifact_id

    adopted_record = registry.get(adopted.adopted_artifact_id)
    assert adopted_record is not None
    adopted_payload = json.loads(Path(adopted_record.path).read_text(encoding="utf-8"))["payload"]
    assert adopted_payload["adoption_identity"] == adopted.adopted_artifact_id
    assert adopted_payload["current_pointer_artifact_id"] == ADOPTION_POINTER_ARTIFACT_ID
