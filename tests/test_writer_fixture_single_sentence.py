from __future__ import annotations

import re
from typing import Any

from services.sentence_segmenter import segment_sentences
from services.writer_task_scope import validate_writer_task_output_v1
from tests.test_current_runtime_full_e2e import _source_bound_writer_response

_CURRENT52_CLAIMS = (
    (
        "Within the recorded source boundaries, rfs-paper-0003 reports The treatment reduced the primary outcome by 2 points in the observed controlled sample (n=24); Results apply only to the observed controlled sample and the 30-day follow-up; they do not establish a longer-term effect; Observed controlled sample; 30-day follow-up; One observed controlled sample (n=24). whereas rfs-paper-0006 reports The treatment reduced the primary outcome by 1 points in the observed controlled sample (n=24); Results apply only to the observed controlled sample and the 30-day follow-up; they do not establish a longer-term effect; Observed controlled sample; 30-day follow-up; One observed controlled sample (n=24)."
    ),
    (
        "Within the recorded source boundaries, rfs-paper-0000 reports The treatment increased the primary outcome by 4 points in the observed controlled sample (n=24). "
        "\u4e2d\u6587\u539f\u6587\uff1a\u89c2\u5bdf\u5230\u7684\u6837\u672c\u4e2d\uff0c\u4e3b\u8981\u7ed3\u679c\u63d0\u9ad8 4 \u4e2a\u70b9; "
        "Results apply only to the observed controlled sample and the 30-day follow-up; they do not establish a longer-term effect. "
        "The two source-labeled samples remain distinct; Observed controlled sample; 30-day follow-up; "
        "The article reports two separately labeled samples, Study A (n=24) and Study B (n=31); no common pooled effect is reported. "
        "whereas rfs-paper-0001 reports The treatment increased the primary outcome by 2 points in the observed controlled sample (n=24); "
        "Results apply only to the observed controlled sample and the 30-day follow-up; they do not establish a longer-term effect; "
        "Observed controlled sample; 30-day follow-up; One observed controlled sample (n=24)."
    ),
)


def _task(index: int, claim: str) -> dict[str, Any]:
    if index == 0:
        task_id = "wtv1_b1ed604dd60606981883437c"
        output_unit_id = "wou1_c19a45a5b4b5acfd739f6d16"
        paper_keys = ["rfspaper0003_unknown_author", "rfspaper0006_unknown_author"]
        ref_ids = ["R004", "R007"]
    else:
        task_id = "wtv1_d934f3e2580682b8374e1d58"
        output_unit_id = "wou1_1547fc6d1b3b92ae2f2045a6"
        paper_keys = ["rfspaper0000_unknown_author", "rfspaper0001_unknown_author"]
        ref_ids = ["R001", "R002"]
    return {
        "writer_task_id": task_id,
        "status": "ready",
        "paper_keys": paper_keys,
        "planned_claim": claim,
        "allowed_ref_ids": ref_ids,
        "source_claim_ids": [f"claim:{index}:{item}" for item in range(12)],
        "evidence_ids": [f"evidence:{index}:{item}" for item in range(12)],
        "source_field_ids": [f"source-field:{index}:{item}" for item in range(16)],
        "qualifier_source_claim_ids": [f"qualifier-claim:{index}:{item}" for item in range(4)],
        "qualifier_evidence_ids": [f"qualifier-evidence:{index}:{item}" for item in range(4)],
        "qualifier_source_field_ids": [f"qualifier-field:{index}:{item}" for item in range(4)],
        "output_units": [{
            "unit_kind": "planned_claim",
            "required": True,
            "writer_output_unit_id": output_unit_id,
            "max_text_chars": 4096,
            "max_text_utf8_bytes": 4096 * 4,
            "allowed_ref_ids": ref_ids,
        }],
    }


def _claim_tokens(value: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]+", value)]


def test_current52_multisentence_claims_fit_one_writer_unit_without_losing_closure() -> None:
    tasks = [_task(index, claim) for index, claim in enumerate(_CURRENT52_CLAIMS)]
    scope = {
        "schema_version": "writer_task_scope_wire/v2",
        "usable_for_provider_admission": True,
        "writer_task_basis_hash": "a" * 64,
        "evidence_store": {
            f"evidence:{index}": {"kind": "source_evidence"}
            for index in range(2)
        },
        "tasks": tasks,
    }
    trace: list[dict[str, Any]] = []

    response = _source_bound_writer_response({"writer_task_scope": scope}, trace)

    assert response is not None
    content = response["content"]
    assert len(content["blocks"]) == 2
    assert [row["writer_output_unit_id"] for row in content["blocks"]] == [
        task["output_units"][0]["writer_output_unit_id"] for task in tasks
    ]
    validation_scope = {
        "schema_version": "writer_task_scope/v1",
        "writer_task_basis_hash": scope["writer_task_basis_hash"],
        "usable_for_provider_admission": True,
        "max_output_units": len(tasks),
        "tasks": tasks,
    }
    assert validate_writer_task_output_v1(validation_scope, content)["scope_status"] == "ready"

    trace_tasks = {row["writer_task_id"]: row for row in trace[0]["tasks"]}
    for task, block in zip(tasks, content["blocks"]):
        emitted = str(block["text"])
        assert len(segment_sentences(emitted)) == 1
        without_citation = re.sub(r"\s*\[\[cite_ref:[^\]]+\]\]\.\s*$", "", emitted).strip()
        assert _claim_tokens(without_citation) == _claim_tokens(task["planned_claim"])
        assert re.findall(r"\[\[cite_ref:([^\]]+)\]\]", emitted) == [", ".join(task["allowed_ref_ids"])]
        traced = trace_tasks[task["writer_task_id"]]
        for field in (
            "source_claim_ids",
            "evidence_ids",
            "source_field_ids",
            "qualifier_source_claim_ids",
            "qualifier_evidence_ids",
            "qualifier_source_field_ids",
            "allowed_ref_ids",
        ):
            assert traced[field] == task[field]

    # The four-sentence multilingual/multi-study assertion retains both study
    # labels, both samples, the separate-study/no-pooling qualifier, and both
    # source citations while becoming one sentence for the existing output unit.
    multilingual = content["blocks"][1]["text"]
    assert "Study A (n=24)" in multilingual and "Study B (n=31)" in multilingual
    assert "no common pooled effect is reported" in multilingual
    assert "\u4e2d\u6587\u539f\u6587" in multilingual
    assert "[[cite_ref:R001, R002]]" in multilingual
