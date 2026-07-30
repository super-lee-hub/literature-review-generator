from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from services.artifact_registry import file_sha256


REPLAY_MANIFEST_SCHEMA = "outline_v2_subagent_response_manifest_v1"
REPLAY_REQUEST_SCHEMA = "outline_v2_subagent_request_v1"
REPLAY_RAW_OUTPUT_SCHEMA = "outline_v2_subagent_raw_output_v1"


class OutlineV2ReplayError(RuntimeError):
    pass


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReplayArtifactBinding:
    artifact_kind: str
    call_index: int
    path: str
    content_hash: str


class OutlineV2ReplayCaller:
    """Replay hash-bound Codex subagent responses through the Outline v2 caller seam."""

    def __init__(self, manifest_path: str | Path) -> None:
        self.manifest_path = str(Path(manifest_path).expanduser().resolve())
        self._manifest = self._load_json(self.manifest_path, label="replay manifest")
        self._entries = self._validate_manifest(self._manifest)
        self._next_index = 1

    @staticmethod
    def _load_json(path: str | Path, *, label: str) -> Mapping[str, Any]:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise OutlineV2ReplayError(f"{label} is missing: {target}")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OutlineV2ReplayError(f"{label} is not valid JSON: {target}") from exc
        if not isinstance(payload, Mapping):
            raise OutlineV2ReplayError(f"{label} must be a JSON object: {target}")
        return payload

    @staticmethod
    def _required_text(payload: Mapping[str, Any], field: str, *, label: str) -> str:
        value = str(payload.get(field) or "").strip()
        if not value:
            raise OutlineV2ReplayError(f"{label} is missing {field}")
        return value

    def _validate_manifest(
        self,
        manifest: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        if (
            manifest.get("artifact_type")
            != "outline_v2_subagent_response_manifest"
            or manifest.get("artifact_version") != "v1"
        ):
            raise OutlineV2ReplayError(
                "Outline v2 replay manifest artifact contract is not recognized"
            )
        if manifest.get("schema_version") != REPLAY_MANIFEST_SCHEMA:
            raise OutlineV2ReplayError("Outline v2 replay manifest schema is not recognized")
        if manifest.get("status") != "complete":
            raise OutlineV2ReplayError("Outline v2 replay manifest is not complete")
        raw_entries = manifest.get("entries")
        if not isinstance(raw_entries, list):
            raise OutlineV2ReplayError("Outline v2 replay manifest entries must be an array")
        try:
            expected_count = int(manifest.get("expected_call_count"))
        except (TypeError, ValueError) as exc:
            raise OutlineV2ReplayError(
                "Outline v2 replay manifest expected_call_count is invalid"
            ) from exc
        if expected_count != len(raw_entries):
            raise OutlineV2ReplayError(
                "Outline v2 replay manifest call count does not match its entries"
            )

        entries: list[Mapping[str, Any]] = []
        for expected_index, raw_entry in enumerate(raw_entries, start=1):
            if not isinstance(raw_entry, Mapping):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay entry {expected_index} is not an object"
                )
            try:
                call_index = int(raw_entry.get("call_index"))
            except (TypeError, ValueError) as exc:
                raise OutlineV2ReplayError(
                    f"Outline v2 replay entry {expected_index} has an invalid call_index"
                ) from exc
            if call_index != expected_index:
                raise OutlineV2ReplayError(
                    "Outline v2 replay entries must be contiguous and ordered"
                )
            for field in (
                "route",
                "stage",
                "prompt_sha256",
                "metadata_sha256",
                "request_path",
                "request_sha256",
                "raw_output_path",
                "raw_output_sha256",
                "subagent_run_id",
            ):
                self._required_text(
                    raw_entry,
                    field,
                    label=f"Outline v2 replay entry {expected_index}",
                )
            entries.append(raw_entry)
        return tuple(entries)

    @property
    def expected_call_count(self) -> int:
        return len(self._entries)

    @property
    def consumed_call_count(self) -> int:
        return self._next_index - 1

    @property
    def subagent_run_ids(self) -> tuple[str, ...]:
        return tuple(str(entry["subagent_run_id"]) for entry in self._entries)

    @property
    def artifact_bindings(self) -> tuple[ReplayArtifactBinding, ...]:
        bindings: list[ReplayArtifactBinding] = []
        for entry in self._entries:
            call_index = int(entry["call_index"])
            bindings.extend(
                (
                    ReplayArtifactBinding(
                        artifact_kind="request",
                        call_index=call_index,
                        path=str(Path(str(entry["request_path"])).expanduser().resolve()),
                        content_hash=str(entry["request_sha256"]),
                    ),
                    ReplayArtifactBinding(
                        artifact_kind="response",
                        call_index=call_index,
                        path=str(Path(str(entry["raw_output_path"])).expanduser().resolve()),
                        content_hash=str(entry["raw_output_sha256"]),
                    ),
                )
            )
        return tuple(bindings)

    def verify_artifacts(self) -> None:
        for entry in self._entries:
            call_index = int(entry["call_index"])
            request_path = Path(str(entry["request_path"])).expanduser().resolve()
            raw_output_path = Path(str(entry["raw_output_path"])).expanduser().resolve()
            if not request_path.is_file() or file_sha256(request_path) != str(
                entry["request_sha256"]
            ):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay request is missing or stale at call {call_index}"
                )
            if not raw_output_path.is_file() or file_sha256(raw_output_path) != str(
                entry["raw_output_sha256"]
            ):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay response is missing or stale at call {call_index}"
                )

            request = self._load_json(request_path, label=f"replay request {call_index}")
            if (
                request.get("artifact_type") != "outline_v2_subagent_request"
                or request.get("artifact_version") != "v1"
                or request.get("schema_version") != REPLAY_REQUEST_SCHEMA
            ):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay request schema mismatch at call {call_index}"
                )
            for field in (
                "call_index",
                "route",
                "stage",
                "prompt_sha256",
                "metadata_sha256",
            ):
                expected = entry[field]
                if request.get(field) != expected:
                    raise OutlineV2ReplayError(
                        f"Outline v2 replay request binding mismatch at call {call_index}"
                    )
            if prompt_sha256(str(request.get("prompt") or "")) != str(
                entry["prompt_sha256"]
            ) or canonical_json_sha256(request.get("metadata")) != str(
                entry["metadata_sha256"]
            ):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay request content mismatch at call {call_index}"
                )

            raw_output = self._load_json(
                raw_output_path,
                label=f"replay response {call_index}",
            )
            if (
                raw_output.get("artifact_type") != "outline_v2_subagent_raw_output"
                or raw_output.get("artifact_version") != "v1"
                or raw_output.get("schema_version") != REPLAY_RAW_OUTPUT_SCHEMA
            ):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay response schema mismatch at call {call_index}"
                )
            if (
                raw_output.get("request_sha256") != str(entry["request_sha256"])
                or str(raw_output.get("subagent_run_id") or "")
                != str(entry["subagent_run_id"])
                or "response" not in raw_output
            ):
                raise OutlineV2ReplayError(
                    f"Outline v2 replay response binding mismatch at call {call_index}"
                )

    def __call__(self, route: str, prompt: str, metadata: Mapping[str, Any]) -> Any:
        if self._next_index > len(self._entries):
            raise OutlineV2ReplayError(
                "Outline v2 requested more model calls than the replay manifest contains"
            )
        entry = self._entries[self._next_index - 1]
        call_index = self._next_index
        self._next_index += 1

        stage = str(metadata.get("stage") or "outline_v2")
        if str(entry["route"]) != str(route) or str(entry["stage"]) != stage:
            raise OutlineV2ReplayError(
                f"Outline v2 replay route/stage mismatch at call {call_index}"
            )
        if str(entry["prompt_sha256"]) != prompt_sha256(prompt):
            raise OutlineV2ReplayError(
                f"Outline v2 replay prompt hash mismatch at call {call_index}"
            )
        if str(entry["metadata_sha256"]) != canonical_json_sha256(dict(metadata)):
            raise OutlineV2ReplayError(
                f"Outline v2 replay metadata hash mismatch at call {call_index}"
            )

        request_path = Path(str(entry["request_path"])).expanduser().resolve()
        raw_output_path = Path(str(entry["raw_output_path"])).expanduser().resolve()
        if not request_path.is_file() or file_sha256(request_path) != str(
            entry["request_sha256"]
        ):
            raise OutlineV2ReplayError(
                f"Outline v2 replay request is missing or stale at call {call_index}"
            )
        if not raw_output_path.is_file() or file_sha256(raw_output_path) != str(
            entry["raw_output_sha256"]
        ):
            raise OutlineV2ReplayError(
                f"Outline v2 replay response is missing or stale at call {call_index}"
            )

        request = self._load_json(request_path, label=f"replay request {call_index}")
        if request.get("schema_version") != REPLAY_REQUEST_SCHEMA:
            raise OutlineV2ReplayError(
                f"Outline v2 replay request schema mismatch at call {call_index}"
            )
        request_checks = {
            "call_index": call_index,
            "route": str(route),
            "stage": stage,
            "prompt_sha256": str(entry["prompt_sha256"]),
            "metadata_sha256": str(entry["metadata_sha256"]),
        }
        if any(request.get(field) != value for field, value in request_checks.items()):
            raise OutlineV2ReplayError(
                f"Outline v2 replay request binding mismatch at call {call_index}"
            )
        if request.get("prompt") != prompt or canonical_json_sha256(
            request.get("metadata")
        ) != str(entry["metadata_sha256"]):
            raise OutlineV2ReplayError(
                f"Outline v2 replay request content mismatch at call {call_index}"
            )

        raw_output = self._load_json(
            raw_output_path,
            label=f"replay response {call_index}",
        )
        if raw_output.get("schema_version") != REPLAY_RAW_OUTPUT_SCHEMA:
            raise OutlineV2ReplayError(
                f"Outline v2 replay response schema mismatch at call {call_index}"
            )
        if raw_output.get("request_sha256") != str(entry["request_sha256"]):
            raise OutlineV2ReplayError(
                f"Outline v2 replay response request hash mismatch at call {call_index}"
            )
        if str(raw_output.get("subagent_run_id") or "") != str(
            entry["subagent_run_id"]
        ):
            raise OutlineV2ReplayError(
                f"Outline v2 replay subagent binding mismatch at call {call_index}"
            )
        if "response" not in raw_output:
            raise OutlineV2ReplayError(
                f"Outline v2 replay response payload is missing at call {call_index}"
            )
        return raw_output["response"]

    def assert_consumed(self) -> None:
        if self.consumed_call_count != self.expected_call_count:
            raise OutlineV2ReplayError(
                "Outline v2 replay manifest contains unused model responses"
            )
