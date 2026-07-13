"""Single downstream outline-loading entry for review/section generation.

All downstream consumption must use OutlineRuntimeResolver.resolve_for_review().
No independent path-guessing in main.py or outline modules.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Dict, Optional

from outline.v2_config import OutlineV2Config
from outline.v2_models import (
    AdoptedFinalOutline,
    compute_content_hash,
)
from services.artifact_registry import file_sha256


@dataclass(frozen=True)
class ResolveResult:
    """Result from OutlineRuntimeResolver.resolve_for_review()."""
    source_path: str
    source_artifact_id: str
    source_artifact_type: str
    markdown: str
    mode: str  # "v2" | "legacy"
    metadata: Dict[str, Any]


class OutlineRuntimeResolver:
    """Single public downstream outline-loading entry.

    Rules:
    - V2 enabled: load only current/registered adopted_final_outline.json,
      verify identity/hash, project to Markdown, fail closed if missing/stale.
    - V2 disabled: load current registered Markdown outline through existing
      compatibility behavior.
    - No independent path-guessing for downstream review.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        artifact_registry: Any = None,
        workspace_path: str = "",
        project_name: str = "",
        legacy_outline_path: str = "",
    ):
        self._config = config
        self._registry = artifact_registry
        self._workspace_path = workspace_path
        self._project_name = project_name
        self._legacy_outline_path = legacy_outline_path

    @property
    def v2_enabled(self) -> bool:
        return OutlineV2Config.from_config(self._config).enable_outline_intelligence_v2

    def resolve_for_review(self) -> Optional[ResolveResult]:
        """Resolve the outline to use for review generation.

        Returns None if no valid outline is available.
        """
        if self.v2_enabled:
            return self._resolve_v2()
        return self._resolve_legacy()

    def _resolve_v2(self) -> Optional[ResolveResult]:
        """V2 mode: load adopted_final_outline.json only. Fail closed."""
        adopted_path = self._resolve_adopted_outline_path()
        if not adopted_path or not os.path.exists(adopted_path):
            return None  # Fail closed — no legacy fallback

        try:
            with open(adopted_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            adopted = AdoptedFinalOutline.from_dict(data)
        except Exception:
            return None  # Corrupt adopted outline — fail closed

        if adopted.artifact_type != "adopted_final_outline" or adopted.artifact_version != "v1":
            return None

        current_final_hash = compute_content_hash(adopted.outline.to_dict())
        source_hash_ok = adopted.source_final_outline_hash == current_final_hash
        if not source_hash_ok and adopted.outline.adoption_status == "adopted":
            source_projection = replace(
                adopted.outline,
                adoption_status="pending_user_adoption",
            )
            source_hash_ok = (
                adopted.source_final_outline_hash
                == compute_content_hash(source_projection.to_dict())
            )
        if not adopted.source_final_outline_hash or not source_hash_ok:
            return None

        # Verify hash identity if registry is available
        if self._registry:
            record = self._registry.get("adopted_final_outline")
            if record and record.status != "ready":
                return None  # Unready adopted outline
            if record:
                if os.path.abspath(record.path) != os.path.abspath(adopted_path):
                    return None
                if record.content_hash and record.content_hash != file_sha256(adopted_path):
                    return None

        markdown = adopted.to_markdown()

        return ResolveResult(
            source_path=adopted_path,
            source_artifact_id="adopted_final_outline",
            source_artifact_type="adopted_final_outline",
            markdown=markdown,
            mode="v2",
            metadata={
                "source_final_outline_hash": adopted.source_final_outline_hash,
                "source_coverage_audit_hash": adopted.source_coverage_audit_hash,
                "adopted_by": adopted.adopted_by,
                "adopted_at": adopted.adopted_at,
            },
        )

    def _resolve_legacy(self) -> Optional[ResolveResult]:
        """Legacy mode: load registered Markdown outline through existing behavior."""
        outline_file = self._legacy_outline_path
        if not outline_file or not os.path.exists(outline_file):
            # Try registry
            if self._registry:
                record = self._registry.get("literature_review_outline")
                if record and record.status == "ready" and os.path.exists(record.path):
                    outline_file = record.path
        if not outline_file or not os.path.exists(outline_file):
            return None

        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                markdown = f.read()
        except Exception:
            return None

        return ResolveResult(
            source_path=outline_file,
            source_artifact_id="literature_review_outline",
            source_artifact_type="literature_review_outline",
            markdown=markdown,
            mode="legacy",
            metadata={"source": "legacy_markdown"},
        )

    def _resolve_adopted_outline_path(self) -> str:
        """Determine the path to adopted_final_outline.json."""
        if self._registry:
            record = self._registry.get("adopted_final_outline")
            if record and record.status == "ready" and os.path.exists(record.path):
                return record.path
            # Once a Registry is bound it is the identity authority.  An
            # unregistered convention file must never become canonical merely
            # because it exists on disk.
            return ""
        # Fallback to workspace convention
        if self._workspace_path and self._project_name:
            return os.path.join(
                self._workspace_path, "artifacts",
                f"{self._project_name}_adopted_final_outline.json",
            )
        return ""
