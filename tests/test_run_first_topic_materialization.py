"""Exact source path and evidence ownership in topic materialization."""

from copy import deepcopy

import pytest

from outline.v3_executor import OutlineV3Executor
from outline.v3_models import SourceFieldLedgerEntry


class _Dossier:
    dossier_id = "dossier:source-a"
    content_hash = "a" * 64

    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return deepcopy(self.payload)


@pytest.mark.parametrize("path,canonical,included", [
    ("root.branch", "different", True),
    ("root.branch.child", "different", True),
    ("root.branch[2].child", "different", True),
    ("ai_summary.root.branch.child", "different", True),
    ("root.branching.child", "different", False),
    ("unrelated.path", "focus", True),
    ("unrelated.path", "different", False),
])
def test_source_path_projection_keeps_exact_roots_and_field_boundaries(path, canonical, included):
    field = SourceFieldLedgerEntry(
        source_field_id="field:source-a:boundary", source_path=path,
        source_value="Only the observed sample at day30.",
        disposition="exact", canonical_field=canonical, scope="paper",
    )
    view = {
        "paper_key": "source-a", "source_summary_hash": "b" * 64,
        "source_fields": {"focus": ["root.branch"]}, "focus": ["Source finding."],
    }
    dossier = _Dossier({
        "claims": [], "research_units": [], "source_field_ledger": [field.to_dict()],
        "evidence_ids_by_field": {
            "focus": ["evidence:exact", "evidence:other-value"],
            "study:a:focus": ["evidence:scoped"],
            "focus-other": ["evidence:foreign-field"],
        },
        "evidence_text_by_id": {
            "evidence:exact": field.source_value, "evidence:scoped": field.source_value,
            "evidence:other-value": "A different source condition.",
            "evidence:foreign-field": field.source_value,
        },
    })
    unit = OutlineV3Executor._complete_topic_evidence_unit(view, dossier, fields=["focus"])
    assert unit["semantic_fields"] == {"focus": ["Source finding."]}
    assert unit["source_summary_hash"] == view["source_summary_hash"]
    if included:
        assert len(unit["source_field_ledger"]) == 1
        row = unit["source_field_ledger"][0]
        assert row["source_field_id"] == field.source_field_id
        assert row["source_value"] == field.source_value
        assert row["projected_fields"] == ["focus"]
        assert row["evidence_ids"] == ["evidence:exact", "evidence:scoped"]
    else:
        assert unit["source_field_ledger"] == []
