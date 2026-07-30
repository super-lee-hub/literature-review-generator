from __future__ import annotations

import json

from free_mode.profile_manager import DEFAULT_PROFILE, build_profile_context
from scripts import pph_final_contracts as contracts


def test_extracts_exact_contract_bodies_and_excludes_audit() -> None:
    source = contracts.SOURCE_PATH.read_text(encoding="utf-8-sig")
    extracted, audit = contracts.extract_contracts(source)

    assert tuple(extracted) == contracts.TOPIC_ORDER
    assert audit.startswith(contracts.AUDIT_MARKER)
    for topic_id, body in extracted.items():
        assert body.startswith(contracts.SCHEMA_GUARD)
        assert f"===== {topic_id}_CONTRACT =====" not in body
        assert f"===== END {topic_id}_CONTRACT =====" not in body
        assert contracts.AUDIT_MARKER not in body


def test_profile_contains_only_the_exact_contract_in_generated_prompt() -> None:
    source = contracts.SOURCE_PATH.read_text(encoding="utf-8-sig")
    extracted, audit = contracts.extract_contracts(source)
    for topic_id, body in extracted.items():
        profile = contracts.build_contract_profile(body)
        assert profile["generated_prompt"] == body
        for key, empty_value in DEFAULT_PROFILE.items():
            if key != "generated_prompt":
                assert profile[key] == empty_value
        assert audit not in profile["generated_prompt"]
        assert build_profile_context(profile)
        assert all(
            other_body not in profile["generated_prompt"]
            for other_id, other_body in extracted.items()
            if other_id != topic_id
        )


def test_prepared_provenance_hashes_read_back_exactly() -> None:
    payload = contracts.prepare_final_contracts()
    for topic_id, item in payload["topics"].items():
        contract_text = contracts.Path(item["contract_path"]).read_text(encoding="utf-8")
        profile = json.loads(contracts.Path(item["profile_path"]).read_text(encoding="utf-8"))
        prompt_context = build_profile_context(profile)
        assert profile["generated_prompt"] == contract_text
        assert item["contract_text_sha256"] == contracts._sha256_text(contract_text)
        assert item["profile_file_sha256"] == contracts._sha256_file(
            contracts.Path(item["profile_path"])
        )
        assert item["prompt_context_sha256"] == contracts._sha256_text(prompt_context)
        assert topic_id in contracts.TOPIC_ORDER
