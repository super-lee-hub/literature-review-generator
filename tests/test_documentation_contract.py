from __future__ import annotations

from pathlib import Path

import main
from outline.stage_health import OUTLINE_STAGE_HEALTH_TYPE, OUTLINE_STAGE_HEALTH_VERSION
from services.artifact_registry import REGISTRY_VERSION
from services.audit_record import AUDIT_SCHEMA_VERSION
from services.job_outcome import JOB_OUTCOME_ARTIFACT_TYPE, JOB_OUTCOME_ARTIFACT_VERSION
from services.source_inventory import SourceInventoryV1
from validation.run_result import VALIDATION_RUN_ARTIFACT_TYPE, VALIDATION_RUN_ARTIFACT_VERSION


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _artifact_filename(artifact_type: str, artifact_version: str) -> str:
    return f"{artifact_type}_{artifact_version}.json"


def _versioned_class_name(base: str, version: str) -> str:
    assert version.startswith("v")
    return f"{base}{version.upper()}"


def test_truth_sources_name_current_canonical_artifacts() -> None:
    for path in (
        "docs/en/runtime/truth-sources.md",
        "docs/zh-CN/runtime/truth-sources.md",
    ):
        text = _read(path)
        for fact in (
            "source_inventory_v1.json",
            "job_outcome_v1.json",
            "ValidationRunResultV1",
            "outline_stage_health_v1.json",
            "adopted_final_outline",
            "external_job",
        ):
            assert fact in text, (path, fact)
        assert "validation_report.json` +" not in text


def test_feature_matrix_does_not_repeat_implemented_features_as_roadmap() -> None:
    for path in (
        "docs/en/reference/feature-matrix.md",
        "docs/zh-CN/reference/feature-matrix.md",
    ):
        text = _read(path)
        assert "AgentRuntimeRunner" in text
        assert "ValidationRunResultV1" in text
        assert "P0:" not in text
        assert "P1:" not in text


def test_compatibility_docs_are_fail_closed_and_audited() -> None:
    for path in (
        "docs/en/runtime/compatibility.md",
        "docs/zh-CN/runtime/compatibility.md",
    ):
        text = _read(path)
        assert "legacy_unverified" in text
        assert "AuditRecordV1" in text
        assert "canonical_ready" in text
        assert "external_job" in text
        assert "migrate-legacy" in text
        assert "--actor" in text
        assert "--reason" in text
        assert "reconcile" in text


def test_schema_and_artifact_versions_match_bilingual_documentation() -> None:
    validation_result_name = _versioned_class_name(
        "ValidationRunResult",
        VALIDATION_RUN_ARTIFACT_VERSION,
    )
    source_inventory_name = _versioned_class_name(
        "SourceInventory",
        SourceInventoryV1.ARTIFACT_VERSION,
    )
    audit_record_name = _versioned_class_name("AuditRecord", AUDIT_SCHEMA_VERSION)

    truth_source_facts = (
        _artifact_filename(SourceInventoryV1.ARTIFACT_TYPE, SourceInventoryV1.ARTIFACT_VERSION),
        f"artifact_registry.json` {REGISTRY_VERSION}",
        _artifact_filename(JOB_OUTCOME_ARTIFACT_TYPE, JOB_OUTCOME_ARTIFACT_VERSION),
        _artifact_filename(OUTLINE_STAGE_HEALTH_TYPE, OUTLINE_STAGE_HEALTH_VERSION),
        f"review_draft_{main.LiteratureReviewGenerator.REVIEW_DRAFT_V2_ARTIFACT_VERSION}.json",
        f"citation_manifest_{main.LiteratureReviewGenerator.CITATION_MANIFEST_ARTIFACT_VERSION}.json",
        _artifact_filename(VALIDATION_RUN_ARTIFACT_TYPE, VALIDATION_RUN_ARTIFACT_VERSION),
        validation_result_name,
    )
    for path in (
        "docs/en/runtime/truth-sources.md",
        "docs/zh-CN/runtime/truth-sources.md",
    ):
        text = _read(path)
        for fact in truth_source_facts:
            assert fact in text, (path, fact)

    feature_facts = (
        f"Artifact Registry {REGISTRY_VERSION}",
        f"review draft {main.LiteratureReviewGenerator.REVIEW_DRAFT_V2_ARTIFACT_VERSION}",
        f"manifest {main.LiteratureReviewGenerator.CITATION_MANIFEST_ARTIFACT_VERSION}",
        validation_result_name,
    )
    for path in (
        "docs/en/reference/feature-matrix.md",
        "docs/zh-CN/reference/feature-matrix.md",
    ):
        text = _read(path)
        for fact in feature_facts:
            assert fact in text, (path, fact)

    compatibility_facts = (
        source_inventory_name,
        f"{REGISTRY_VERSION.upper()} dependency identity",
        validation_result_name,
        audit_record_name,
    )
    for path in (
        "docs/en/runtime/compatibility.md",
        "docs/zh-CN/runtime/compatibility.md",
    ):
        text = _read(path)
        for fact in compatibility_facts:
            assert fact in text, (path, fact)


def test_mineru_external_host_allowlist_is_documented_safely() -> None:
    english = _read("README.en.md")
    chinese = _read("README.zh-CN.md")
    env_example = _read(".env.example")

    assert "MINERU_ALLOWED_URL_HOSTS" in english
    assert "comma-separated exact hostnames" in english
    assert "scheme, path, or wildcard" in english
    assert "MINERU_ALLOWED_URL_HOSTS" in chinese
    assert "逗号分隔的精确主机名" in chinese
    assert "协议、路径或通配符" in chinese
    assert "MINERU_ALLOWED_URL_HOSTS=" in env_example
