import types
import time
import configparser

from validation.review_validator import CitationValidationResult, ReviewValidationReport, RootCause, ValidationConclusion
import validator


class _DummyLogger:
    def info(self, _msg):
        pass

    def warning(self, _msg):
        pass

    def error(self, _msg):
        pass

    def success(self, _msg):
        pass


class _DummyConfig(dict):
    def getboolean(self, _section, _option, fallback=False):
        return True


def _base_result() -> CitationValidationResult:
    return CitationValidationResult(
        citation_id="cite-1",
        paper_id="paper-1",
        conclusion=ValidationConclusion.PARTIAL_SUPPORT,
        root_causes=[RootCause.INSUFFICIENT_CONTEXT],
        evidence_candidates=[],
        details={
            "claim_type": "synthesis",
            "claim_type_confidence": 0.75,
            "claim_type_rationale": "multi-paper synthesis cue",
            "per_paper_evidence_packets": {"paper-1": []},
        },
        claim_text="Claim text",
        claim_context="Section 1",
        evidence_excerpt_list=["Evidence excerpt"],
        reasoning_summary="Needs adjudication",
        repair_hint="Narrow if needed",
        citation_set_key="paper-1",
        paper_ids=["paper-1"],
        block_ids=["block-1"],
        low_confidence=False,
        evidence_status="evidence_gap",
        disposition="manual_review",
    )


def _base_result_with_key(key: str) -> CitationValidationResult:
    result = _base_result()
    result.citation_id = key
    result.citation_set_key = key
    result.paper_id = key
    result.paper_ids = [key]
    result.details["per_paper_evidence_packets"] = {key: []}
    return result


def _dummy_generator():
    return types.SimpleNamespace(
        logger=_DummyLogger(),
        config=_DummyConfig({"Validator_API": {"api_key": "dummy", "model": "dummy"}}),
        summaries=[],
    )


def _packet_builder(result, stage="primary"):
    return types.SimpleNamespace(
        stage=stage,
        claim_text=result.claim_text,
        paper_ids=result.paper_ids,
        claim_type=result.details.get("claim_type", "result"),
        claim_type_confidence=result.details.get("claim_type_confidence", 0.0),
        claim_type_rationale=result.details.get("claim_type_rationale", ""),
        claim_context=result.claim_context,
        block_context=result.block_context,
        claim_units=result.claim_units,
        target_claim_unit=result.target_claim_unit,
        claim_unit_results=result.details.get("claim_unit_results", []),
        paper_identity_hints=result.details.get("paper_identity_hints", {}),
        per_paper_evidence_packets=result.details.get("per_paper_evidence_packets", {}),
        evidence_excerpt_list=result.evidence_excerpt_list,
        trimmed_candidate_counts={},
        citation_set_key=result.citation_set_key,
        evidence_status=result.evidence_status,
        disposition=result.disposition,
    )


def _patch_runtime(monkeypatch, initial_result):
    report = ReviewValidationReport(
        report_id="report-1",
        created_at="2026-04-19T00:00:00Z",
        total_citations=1,
        supported_count=0,
        partial_support_count=1,
        unsupported_count=0,
        wrong_source_count=0,
        needs_review_count=0,
        citation_results=[initial_result],
    )

    class _DummyReviewValidator:
        def __init__(self, *_args, **_kwargs):
            pass

        def validate(self):
            return report

    monkeypatch.setattr("validation.review_validator.ReviewValidator", _DummyReviewValidator)
    monkeypatch.setattr(
        validator,
        "_load_validation_inputs",
        lambda _g: ({"content": {"sections": []}}, {"artifact_version": "v3", "citation_sets": [], "paper_entries": []}, [], {}, {}),
    )
    monkeypatch.setattr(validator, "_apply_summary_repairs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(validator, "_apply_review_repairs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        validator,
        "_write_validation_reports",
        lambda *_args, **_kwargs: {"report_file": "report.txt", "manual_report_file": "manual.json"},
    )


def test_run_review_validation_escalates_before_marking_supported(monkeypatch):
    _patch_runtime(monkeypatch, _base_result())
    generator = _dummy_generator()
    stage_order = []

    monkeypatch.setattr(validator, "build_adjudication_packet", _packet_builder)

    def _fake_run_stage(_generator, _api_config, packet):
        stage_order.append(packet.stage)
        if packet.stage == "primary":
            return {
                "status": "evidence_gap",
                "confidence": 0.41,
                "repair_scope": "manual_review",
                "disposition": "manual_review",
                "low_confidence": True,
                "reasoning": "Primary stage remains uncertain.",
                "repair_hint": "Escalate.",
                "manual_review_reason": "Need stronger pass.",
                "adjudication_stage": "primary",
                "adjudication_status": "uncertain",
                "claim_type": "synthesis",
                "claim_type_confidence": 0.75,
            }
        return {
            "status": "supported",
            "confidence": 0.9,
            "repair_scope": "none",
            "disposition": "keep_as_is",
            "low_confidence": False,
            "reasoning": "Stronger stage resolved the bundle.",
            "repair_hint": "",
            "manual_review_reason": "",
            "adjudication_stage": "stronger",
            "adjudication_status": "supported",
            "claim_type": "synthesis",
            "claim_type_confidence": 0.82,
        }

    monkeypatch.setattr(validator, "run_adjudication_stage", _fake_run_stage)

    result = validator.run_review_validation(generator)

    assert result["success"] is True
    assert stage_order == ["primary", "stronger"]
    assert result["manual_review_items"] == []
    final_result = result["report"].citation_results[0]
    assert final_result.details["adjudication_stage"] == "stronger"
    assert final_result.details["escalated"] is True
    assert final_result.conclusion == ValidationConclusion.SUPPORTED


def test_run_review_validation_reaches_manual_review_only_after_stronger_pass(monkeypatch):
    _patch_runtime(monkeypatch, _base_result())
    generator = _dummy_generator()
    stage_order = []

    monkeypatch.setattr(validator, "build_adjudication_packet", _packet_builder)

    def _fake_run_stage(_generator, _api_config, packet):
        stage_order.append(packet.stage)
        return {
            "status": "low_confidence" if packet.stage == "stronger" else "evidence_gap",
            "confidence": 0.35,
            "repair_scope": "manual_review",
            "disposition": "manual_review",
            "low_confidence": True,
            "reasoning": "Still uncertain.",
            "repair_hint": "Manual review required.",
            "manual_review_reason": "Evidence remains insufficient after stronger adjudication.",
            "adjudication_stage": packet.stage,
            "adjudication_status": "uncertain",
            "claim_type": "synthesis",
            "claim_type_confidence": 0.75,
        }

    monkeypatch.setattr(validator, "run_adjudication_stage", _fake_run_stage)

    result = validator.run_review_validation(generator)

    assert result["success"] is True
    assert stage_order == ["primary", "stronger"]
    assert len(result["manual_review_items"]) == 1
    final_result = result["report"].citation_results[0]
    assert final_result.details["adjudication_stage"] == "stronger"
    assert final_result.details["escalated"] is True
    assert final_result.conclusion == ValidationConclusion.NEEDS_REVIEW


def test_run_adjudication_ladder_parallel_preserves_input_order(monkeypatch):
    generator = _dummy_generator()
    citation_results = [
        _base_result_with_key("slow"),
        _base_result_with_key("fast"),
        _base_result_with_key("middle"),
    ]

    def _fake_primary(_generator, result):
        delays = {"slow": 0.03, "fast": 0.0, "middle": 0.01}
        time.sleep(delays[result.citation_set_key])
        return result

    monkeypatch.setattr(validator, "_run_ai_bundle_validation", _fake_primary)
    monkeypatch.setattr(validator, "_needs_stronger_ai_adjudication", lambda _result: False)

    adjudicated = validator._run_adjudication_ladder(generator, citation_results, max_workers=3)

    assert [result.citation_set_key for result in adjudicated] == ["slow", "fast", "middle"]


def test_get_validation_max_workers_reads_configparser_section():
    parser = configparser.ConfigParser()
    parser["Performance"] = {"max_workers": "2"}
    parser["Validation"] = {"max_workers": "4"}
    generator = types.SimpleNamespace(config=parser)

    assert validator._get_validation_max_workers(generator) == 4


def test_get_validation_max_workers_uses_current_default_when_validation_is_invalid():
    generator = types.SimpleNamespace(
        config={
            "Performance": {"max_workers": "3"},
            "Validation": {"max_workers": "0"},
        }
    )

    assert validator._get_validation_max_workers(generator) == 1
