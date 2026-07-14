from dataclasses import replace
import threading

import pytest

from validation.edge_checkpoint import ValidationEdgeCheckpointStore, ValidationEdgeKeyV1
from validation.review_validator import ReviewValidator


def _paper() -> dict:
    return {
        "paper_identity": {"canonical_paper_key": "paper-a", "source_paper_id": "alias-a"},
        "analysis": {
            "preprocess": {
                "normalized_text": "Price unfairness increases complaint behavior.",
                "chunks": [{"chunk_id": "c1", "text": "Price unfairness increases complaint behavior."}],
                "page_index": [{"page": 1, "text": "Price unfairness increases complaint behavior."}],
            }
        },
        "stage1_inputs": {"selected_visual_refs": []},
    }


def _manifest(count: int = 43) -> dict:
    bundles = []
    for index in range(count):
        claim = f"Price unfairness increases complaint behavior {index}."
        bundles.append(
            {
                "bundle_id": f"bundle-{index}",
                "citation_set_key": f"set-{index}",
                "paper_ids": ["alias-a"],
                "paper_keys": ["paper-a"],
                "block_ids": [],
                "section_titles": ["Results"],
                "claim_texts": [claim],
                "claim_units": [
                    {
                        "claim_unit_id": f"claim-{index}",
                        "claim_text": claim,
                        "paper_ids": ["alias-a"],
                        "supporting_paper_ids": ["alias-a"],
                        "alignment_status": "explicit",
                        "block_id": "",
                        "sentence_index": 1,
                        "span_start": 0,
                        "span_end": len(claim),
                    }
                ],
            }
        )
    return {"artifact_version": "v3", "citation_sets": bundles}


def _validator(store, callback=None) -> ReviewValidator:
    return ReviewValidator(
        {"content": {"sections": []}},
        _manifest(),
        [_paper()],
        edge_checkpoint_store=store,
        edge_checkpoint_callback=callback,
    )


def test_serial_resume_after_twenty_durable_edges_runs_exactly_remaining_twenty_three(tmp_path):
    store = ValidationEdgeCheckpointStore(tmp_path / "edges")
    completed = 0

    def interrupt_after_twenty(_key, _path):
        nonlocal completed
        completed += 1
        if completed == 20:
            raise KeyboardInterrupt("fault after twentieth durable edge")

    with pytest.raises(KeyboardInterrupt):
        _validator(store, interrupt_after_twenty).validate(max_workers=1)
    assert len(list((tmp_path / "edges").glob("*.json"))) == 20

    resumed = 0

    def count_new(_key, _path):
        nonlocal resumed
        resumed += 1

    report = _validator(store, count_new).validate(max_workers=1)
    assert report.total_citations == 43
    assert resumed == 23
    assert len(list((tmp_path / "edges").glob("*.json"))) == 43


def test_parallel_resume_only_materializes_edges_without_durable_checkpoint(tmp_path):
    store = ValidationEdgeCheckpointStore(tmp_path / "edges")
    seeded = 0

    def interrupt_after_twenty(_key, _path):
        nonlocal seeded
        seeded += 1
        if seeded == 20:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _validator(store, interrupt_after_twenty).validate(max_workers=1)

    resumed = 0
    lock = threading.Lock()

    def count_new(_key, _path):
        nonlocal resumed
        with lock:
            resumed += 1

    report = _validator(store, count_new).validate(max_workers=8)
    assert report.total_citations == 43
    assert resumed == 23


def test_checkpoint_identity_uses_canonical_key_not_manifest_alias(tmp_path):
    validator = _validator(ValidationEdgeCheckpointStore(tmp_path / "edges"))
    claim_unit = _manifest(1)["citation_sets"][0]["claim_units"][0]
    key = validator._edge_key(
        claim_unit=claim_unit,
        paper_id="alias-a",
        paper_artifact=_paper(),
        retrieval_queries=[claim_unit["claim_text"]],
        segment_coverages=[],
    )
    assert key.canonical_paper_key == "paper-a"


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("claim_unit_hash", "claim-b"),
        ("canonical_paper_key", "paper-b"),
        ("evidence_hashes", ("evidence-b",)),
        ("segmenter_version", "segmenter-v2"),
        ("retrieval_config_hash", "retrieval-b"),
        ("model_route", "provider-b/model-b"),
        ("prompt_version", "prompt-v2"),
        ("adjudication_schema_version", "schema-v2"),
    ],
)
def test_checkpoint_key_changes_when_any_contract_identity_field_changes(
    field_name: str,
    replacement: object,
) -> None:
    baseline = ValidationEdgeKeyV1(
        claim_unit_hash="claim-a",
        canonical_paper_key="paper-a",
        evidence_hashes=("evidence-a",),
        segmenter_version="segmenter-v1",
        retrieval_config_hash="retrieval-a",
        model_route="provider-a/model-a",
        prompt_version="prompt-v1",
        adjudication_schema_version="schema-v1",
    )

    changed = replace(baseline, **{field_name: replacement})

    assert changed.checkpoint_key != baseline.checkpoint_key
