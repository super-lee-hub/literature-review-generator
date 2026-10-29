import json
from copy import deepcopy

import pytest

from outline.evidence_projection import (
    build_pack,
    build_source_closed_pack_v2,
    validate_pack,
)


def _entry():
    return {"status": "success", "source_mode": "direct",
            "paper_info": {"canonical_paper_key": "A", "title": "Study A", "source_pdf": "old/a.pdf",
                           "source_pdf_sha256": "a" * 64, "source_pdf_fingerprint": "a" * 64},
            "ai_summary": {"findings": "No effect among adults.", "qualifiers": ["30 days only.", "观察期仅30天。"]},
            "preprocess": {"output_path": "parent/preprocess"}}


def test_complete_content_identity_survives_verified_reuse_wrapper_and_relocation():
    parent = _entry()
    parent["paper_info"].update({"source_paper_id": "old/a.pdf", "pdf_path": "old/a.pdf",
                                  "source_descriptor": {"source_pdf": "old/a.pdf", "source_paper_id": "old/a.pdf"},
                                  "attachments": ["old/a.pdf", "supplement.pdf"]})
    child = deepcopy(parent)
    child["paper_info"].pop("source_pdf_sha256")
    child["preprocess"] = {"output_path": "child/preprocess"}
    child["stage1_reuse"] = {"origin_job_id": "parent"}
    child["paper_info"].update({"source_pdf": "new/a.pdf", "source_paper_id": "new/a.pdf", "pdf_path": "new/a.pdf",
                                 "source_descriptor": {"source_pdf": "new/a.pdf", "source_paper_id": "new/a.pdf"},
                                 "attachments": ["new/a.pdf", "supplement.pdf"]})
    before = deepcopy((parent, child))
    first = build_source_closed_pack_v2([parent], source_pins={"A": {"source_pdf_sha256": "a" * 64, "source_pdf": "old/a.pdf"}})
    second = build_source_closed_pack_v2([child], source_pins={"A": {"source_pdf_sha256": "a" * 64, "source_pdf": "new/a.pdf"}})
    assert first["entries"] == second["entries"]
    assert first["pack_payload_sha256"] == second["pack_payload_sha256"]
    assert first["source_entry_provenance"] != second["source_entry_provenance"]
    assert first["entries"][0]["ai_summary"] == parent["ai_summary"]
    assert first["entries"][0]["paper_info"]["attachments"][-1] == "supplement.pdf"
    assert first["source_entry_provenance"][0]["original_paper_info"] == parent["paper_info"]
    raw = json.dumps(parent, ensure_ascii=False, sort_keys=True)
    assert first["source_entry_provenance"][0]["source_entry_bytes"] == len(raw.encode("utf-8")) > len(raw)
    assert (parent, child) == before
    assert build_pack([parent])["entries"] != build_pack([child])["entries"]


def test_qualifier_change_invalidates_provider_identity_and_tampered_content_is_rejected():
    source = _entry()
    pins = {"A": {"source_pdf_sha256": "a" * 64}}
    original = build_source_closed_pack_v2([source], source_pins=pins)
    changed = deepcopy(source)
    changed["ai_summary"]["qualifiers"] = ["Adults older than fifty only."]
    revised = build_source_closed_pack_v2([changed], source_pins=pins)
    assert revised["pack_payload_sha256"] != original["pack_payload_sha256"]
    revised["entries"][0]["ai_summary"]["findings"] = "Positive effect."
    with pytest.raises(ValueError, match="semantic identity"):
        validate_pack(revised)
    with pytest.raises(ValueError, match="runtime PDF pin"):
        build_source_closed_pack_v2([source], source_pins={"A": {"source_pdf_sha256": "b" * 64}})
