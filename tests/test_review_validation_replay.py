import json
from pathlib import Path

from validation.review_validator import EvidenceStatus, ReviewValidator, ValidationDisposition, ValidationConclusion
from validator import _build_report_from_results, _map_ai_bundle_result


FIXTURE_ROOT = Path("tests/fixtures/review_validation/first_section_must")


def _load_fixture_bundle():
    review_draft = json.loads((FIXTURE_ROOT / "review_draft_v2.json").read_text(encoding="utf-8"))
    citation_manifest = json.loads((FIXTURE_ROOT / "citation_manifest_v3.json").read_text(encoding="utf-8"))
    paper_artifacts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((FIXTURE_ROOT / "paper_artifacts").glob("*.json"))
    ]
    return review_draft, citation_manifest, paper_artifacts


def test_first_section_must_replay_fixture_is_hermetic() -> None:
    assert FIXTURE_ROOT.exists()
    assert (FIXTURE_ROOT / "review_draft_v2.json").exists()
    assert (FIXTURE_ROOT / "citation_manifest_v3.json").exists()
    assert list((FIXTURE_ROOT / "paper_artifacts").glob("*.json"))


def test_first_section_must_replay_targets_do_not_regress_to_plain_unsupported() -> None:
    review_draft, citation_manifest, paper_artifacts = _load_fixture_bundle()
    report = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate()
    by_key = {result.citation_set_key: result for result in report.citation_results}

    methodology = by_key["10.1016/j.jbusres.2022.03.017"]
    game_theory = by_key["10.1007/s11129-017-9181-1"]
    future = by_key["10.1080/10696679.2025.2552276"]

    assert methodology.conclusion == ValidationConclusion.SUPPORTED
    assert methodology.evidence_status == EvidenceStatus.CLEAN_SUPPORTED.value

    assert game_theory.conclusion == ValidationConclusion.SUPPORTED
    assert game_theory.evidence_status == EvidenceStatus.CLEAN_SUPPORTED.value

    assert future.conclusion != ValidationConclusion.UNSUPPORTED
    assert future.evidence_status in {EvidenceStatus.EVIDENCE_GAP.value, EvidenceStatus.NEEDS_REVIEW.value}
    assert future.disposition in {
        ValidationDisposition.REVIEW_REPAIR.value,
        ValidationDisposition.MANUAL_REVIEW.value,
    }


def test_first_section_must_replay_report_surfaces_repaired_retained_bucket() -> None:
    review_draft, citation_manifest, paper_artifacts = _load_fixture_bundle()
    report = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate()
    by_key = {result.citation_set_key: result for result in report.citation_results}

    narrowed_future = _map_ai_bundle_result(
        by_key["10.1080/10696679.2025.2552276"],
        {
            "status": "supported",
            "confidence": 0.81,
            "repair_scope": "review",
            "disposition": "narrowed_and_kept",
            "low_confidence": False,
            "reasoning": "The claim becomes supportable after narrowing to the subscription-service example.",
            "repair_hint": "Keep only the subscription-service example.",
            "summary_paper_ids": ["10.1080/10696679.2025.2552276"],
        },
    )

    final_report = _build_report_from_results(
        [
            by_key["10.1016/j.jbusres.2022.03.017"],
            by_key["10.1007/s11129-017-9181-1"],
            narrowed_future,
        ]
    )

    assert final_report.supported_count == 2
    assert final_report.narrowed_and_kept_count == 1
    assert final_report.partial_support_count == 1


def test_review_validator_validate_emits_progress_callback_for_each_bundle() -> None:
    review_draft, citation_manifest, paper_artifacts = _load_fixture_bundle()
    seen: list[tuple[int, int, str]] = []

    def _progress(index: int, total: int, bundle: dict) -> None:
        seen.append((index, total, str(bundle.get("citation_set_key") or "")))

    report = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate(progress_callback=_progress)

    assert seen
    assert report.total_citations == len(seen)
    assert seen[0][0] == 1
    assert seen[-1][0] == len(seen)
    assert all(total == len(seen) for _, total, _ in seen)


def test_review_validator_parallel_validate_preserves_report_order() -> None:
    review_draft, citation_manifest, paper_artifacts = _load_fixture_bundle()
    sequential_report = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate()
    seen: list[tuple[int, int, str]] = []

    def _progress(index: int, total: int, bundle: dict) -> None:
        seen.append((index, total, str(bundle.get("citation_set_key") or "")))

    parallel_report = ReviewValidator(review_draft, citation_manifest, paper_artifacts).validate(
        progress_callback=_progress,
        max_workers=2,
    )

    sequential_keys = [result.citation_set_key for result in sequential_report.citation_results]
    parallel_keys = [result.citation_set_key for result in parallel_report.citation_results]
    assert parallel_keys == sequential_keys
    assert parallel_report.total_citations == sequential_report.total_citations
    assert len(seen) == sequential_report.total_citations
    assert sorted(index for index, _, _ in seen) == list(range(1, sequential_report.total_citations + 1))
