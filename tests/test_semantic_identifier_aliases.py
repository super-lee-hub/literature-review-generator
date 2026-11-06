from __future__ import annotations

from copy import deepcopy

import pytest

from outline.semantic_identifier_aliases import (
    IDENTIFIER_FIELDS,
    WIRE_KEY,
    SemanticIdentifierAliasError,
    build_identifier_alias_plan,
    decode_identifier_alias_output,
    expand_identifier_alias_request,
)
from runtime.provider_runtime import hash_json


PAPER = "10.1000/long-canonical-paper-identity"
PRIMARY = "claim:long-canonical-paper-identity:finding:source-hash"
QUALIFIER = "claim:long-canonical-paper-identity:boundary:source-hash"
EVIDENCE = "evidence:long-canonical-paper-identity:boundary:source-hash"
FIELD = "source-field:long-canonical-paper-identity:sample-boundary"
PROSE = "Only the observed sample (n=24) improved; no pooled study effect is established. 中文限定条件必须保留。"


def _request():
    return {
        "task": "substantive_global_synthesis",
        "topic_synthesis": [{
            "topic_id": "topic:source-reported-effects",
            "paper_ids": [PAPER], "bridge_paper_ids": [PAPER],
            "claims": [{"claim_id": PRIMARY, "paper_key": PAPER, "text": PROSE, "evidence_ids": [EVIDENCE]}],
            "interpretation_context": {
                "fields": [{"source_field_id": FIELD, "source_value": PROSE, "study_id": "", "owner_study_id": "", "paper_key": PAPER, "zero": 0, "unknown": False}],
                "dependencies": [{"primary_claim_id": PRIMARY, "required_source_claim_ids": [QUALIFIER], "required_evidence_ids": [EVIDENCE], "required_source_field_ids": [FIELD], "reason": PROSE}],
            },
            "fragment_id": "fragment:condition-a", "relation_ids": ["relation:source-pair"], "result_ids": ["result:source-response"],
        }],
        "cross_group_comparison": {
            "bridge_claims": [{"claim_id": "synthesis:cross_group_comparison:observed", "source_claim_ids": [PRIMARY, QUALIFIER], "source_field_ids": [FIELD], "evidence_ids": [EVIDENCE], "text": PROSE}],
            "topic_dispositions": [{"topic_id": "topic:source-reported-effects", "integrated_paper_ids": [PAPER], "nonintegrated_paper_ids": [], "synthesis_claim_ids": ["synthesis:cross_group_comparison:observed"]}],
            "source_dispositions": [{"assigned_paper_ids": [PAPER], "study_id": "study:explicit-source-one", "owner_study_ids": ["study:explicit-source-one"], "reason": PROSE}],
        },
        "unknown_extra": {"literal": PRIMARY, "prose_containing_id": f"Keep literal {PAPER} in prose.", "zero": 0, "false": False, "empty": "", "none": None, "values": [0, False, "", None]},
    }


def test_alias_plan_roundtrip_preserves_all_prose_qualifiers_values_and_original():
    request = _request()
    before = deepcopy(request)
    catalog, wire = build_identifier_alias_plan(request)
    assert request == before
    assert catalog["source_request_hash"] == hash_json(request)
    assert expand_identifier_alias_request(wire, catalog) == request
    assert hash_json(expand_identifier_alias_request(wire, catalog)) == hash_json(request)
    assert "forward" not in wire[WIRE_KEY] and "reverse" not in wire[WIRE_KEY]
    assert wire["unknown_extra"] == request["unknown_extra"]
    context = wire["topic_synthesis"][0]["interpretation_context"]
    assert context["fields"][0]["source_value"] == PROSE
    assert context["dependencies"][0]["reason"] == PROSE
    assert context["dependencies"][0]["required_source_field_ids"] == [catalog["forward"][FIELD]]
    assert context["fields"][0]["study_id"] == context["fields"][0]["owner_study_id"] == ""
    assert context["fields"][0]["zero"] == 0 and context["fields"][0]["unknown"] is False
    assert wire["topic_synthesis"][0]["paper_ids"] == wire["topic_synthesis"][0]["bridge_paper_ids"] == [catalog["forward"][PAPER]]
    assert catalog["reverse"] == {value: key for key, value in catalog["forward"].items()}


def test_each_named_structural_identifier_field_has_the_same_canonical_alias():
    request = {key: [PRIMARY, "", 0, False, None] if key.endswith("s") else PRIMARY for key in IDENTIFIER_FIELDS}
    catalog, wire = build_identifier_alias_plan(request)
    assert len(catalog["forward"]) == 1
    alias = catalog["forward"][PRIMARY]
    for key in IDENTIFIER_FIELDS:
        assert wire[key] == ([alias, "", 0, False, None] if key.endswith("s") else alias)
    assert hash_json(expand_identifier_alias_request(wire, catalog)) == hash_json(request)


def test_alias_namespace_avoids_input_value_and_key_prefix_collisions():
    request = _request()
    request["$I_key_collision"] = "not an identifier"
    request["collisions"] = ["$I0", "$I_0", "$I__prefix in ordinary prose"]
    catalog, wire = build_identifier_alias_plan(request)
    assert catalog["prefix"] == "$I___"
    assert wire["collisions"] == request["collisions"]
    assert expand_identifier_alias_request(wire, catalog) == request
    assert build_identifier_alias_plan(request) == (catalog, wire)


def test_alias_map_determinism_does_not_depend_on_object_key_order():
    request = _request()
    catalog, _wire = build_identifier_alias_plan(request)
    reordered = dict(reversed(list(request.items())))
    other, _otherwire = build_identifier_alias_plan(reordered)
    assert other == catalog


def test_output_decoder_keeps_new_global_claim_id_and_decodes_complete_support():
    catalog, _wire = build_identifier_alias_plan(_request())
    forward = catalog["forward"]
    output = {"synthesis_claims": [{
        "claim_id": "synthesis:global_synthesis:new-claim",
        "paper_keys": [forward[PAPER]], "source_claim_ids": [forward[PRIMARY], forward[QUALIFIER]],
        "evidence_ids": [forward[EVIDENCE]], "source_field_ids": [forward[FIELD]],
        "text": PROSE, "zero": 0, "uncertainty_resolved": False,
    }], "unknown_literal": forward[PRIMARY]}
    before = deepcopy(output)
    decoded = decode_identifier_alias_output(output, catalog)
    assert output == before
    claim = decoded["synthesis_claims"][0]
    assert claim["claim_id"] == "synthesis:global_synthesis:new-claim"
    assert claim["source_claim_ids"] == [PRIMARY, QUALIFIER]
    assert claim["evidence_ids"] == [EVIDENCE]
    assert claim["source_field_ids"] == [FIELD]
    assert claim["text"] == PROSE and claim["uncertainty_resolved"] is False
    assert decoded["unknown_literal"] == forward[PRIMARY]


def test_decoder_never_rewrites_alias_substrings_or_unknown_field_values():
    catalog, _wire = build_identifier_alias_plan(_request())
    alias = catalog["forward"][PRIMARY]
    unknown_alias = catalog["prefix"] + "999999"
    text = f"The literal tokens {alias} and {unknown_alias} occur in this quoted source text."
    output = {"claim_id": "synthesis:global_synthesis:new", "text": text, "extra_literal": unknown_alias, "zero": 0, "float_zero": 0.0, "false": False}
    decoded = decode_identifier_alias_output(output, catalog)
    assert hash_json(decoded) == hash_json(output)
    assert decoded["text"] == text
    assert type(decoded["zero"]) is int and type(decoded["float_zero"]) is float
    assert decoded["false"] is False


def test_large_identifier_plan_keeps_all_qualifier_text_and_binds_one_local_map():
    request = _request()
    request["topic_synthesis"] = [{
        "topic_id": f"topic:{index}:" + "x" * 100,
        "claims": [{
            "claim_id": f"claim:{index}:" + "y" * 150,
            "paper_key": PAPER,
            "source_field_ids": [FIELD],
            "evidence_ids": [f"evidence:{index}:" + "z" * 150],
            "text": PROSE,
        }],
    } for index in range(100)]
    catalog, wire = build_identifier_alias_plan(request)
    assert len(catalog["forward"]) >= 300
    assert len(hash_json(catalog)) == 64
    assert len(str(wire)) < len(str(request))
    assert all(topic["claims"][0]["text"] == PROSE for topic in wire["topic_synthesis"])
    assert hash_json(expand_identifier_alias_request(wire, catalog)) == hash_json(request)
    assert len(wire[WIRE_KEY]) == 7


@pytest.mark.parametrize("value", ["999999", "00", "-1", "one", "0suffix"])
def test_output_decoder_rejects_unknown_or_malformed_alias(value):
    catalog, _wire = build_identifier_alias_plan(_request())
    with pytest.raises(SemanticIdentifierAliasError, match="unknown structural"):
        decode_identifier_alias_output({"source_claim_ids": [catalog["prefix"] + value]}, catalog)


@pytest.mark.parametrize("mutation", ["false_as_zero", "zero_as_false", "int_as_float", "prose", "missing_qualifier", "unknown_alias", "unaliased_id", "mapping_hash", "source_hash", "policy", "extra_metadata"])
def test_request_expansion_rejects_type_prose_scope_and_authority_changes(mutation):
    catalog, wire = build_identifier_alias_plan(_request())
    if mutation == "false_as_zero":
        wire["unknown_extra"]["false"] = 0
    elif mutation == "zero_as_false":
        wire["unknown_extra"]["zero"] = False
    elif mutation == "int_as_float":
        wire["unknown_extra"]["zero"] = 0.0
    elif mutation == "prose":
        wire["topic_synthesis"][0]["claims"][0]["text"] = "Unqualified replacement."
    elif mutation == "missing_qualifier":
        wire["cross_group_comparison"]["bridge_claims"][0]["source_field_ids"] = []
    elif mutation == "unknown_alias":
        wire["topic_synthesis"][0]["paper_ids"] = [catalog["prefix"] + "999999"]
    elif mutation == "unaliased_id":
        wire["topic_synthesis"][0]["paper_ids"] = [PAPER]
    elif mutation == "mapping_hash":
        wire[WIRE_KEY]["mapping_hash"] = "0" * 64
    elif mutation == "source_hash":
        wire[WIRE_KEY]["source_request_hash"] = "0" * 64
    elif mutation == "policy":
        wire[WIRE_KEY]["policy"] = "unknown-policy"
    else:
        wire[WIRE_KEY]["forward"] = catalog["forward"]
    with pytest.raises(SemanticIdentifierAliasError):
        expand_identifier_alias_request(wire, catalog)


@pytest.mark.parametrize("mutation", ["forward", "reverse", "catalog_hash", "policy", "prefix", "extra"])
def test_both_decoders_reject_a_tampered_local_catalog(mutation):
    catalog, wire = build_identifier_alias_plan(_request())
    if mutation == "forward":
        catalog["forward"][PRIMARY] = "foreign-alias"
    elif mutation == "reverse":
        catalog["reverse"][catalog["forward"][PRIMARY]] = QUALIFIER
    elif mutation in {"catalog_hash", "policy", "prefix"}:
        catalog[mutation] = "changed"
    else:
        catalog["unexpected"] = False
    with pytest.raises(SemanticIdentifierAliasError):
        expand_identifier_alias_request(wire, catalog)
    with pytest.raises(SemanticIdentifierAliasError):
        decode_identifier_alias_output({"claim_id": "synthesis:global_synthesis:new"}, catalog)


def test_no_identifier_request_keeps_an_empty_map_and_preserves_values():
    request = {"text": PROSE, "zero": 0, "false": False, "empty": "", "rows": []}
    catalog, wire = build_identifier_alias_plan(request)
    assert catalog["forward"] == catalog["reverse"] == {}
    assert expand_identifier_alias_request(wire, catalog) == request


@pytest.mark.parametrize("source", [{WIRE_KEY: {}}, {"bad": float("nan")}, {"bad": ("not", "json", "array")}, {1: "nonstring-key"}])
def test_planning_rejects_existing_metadata_and_non_json_data(source):
    with pytest.raises(SemanticIdentifierAliasError):
        build_identifier_alias_plan(source)
