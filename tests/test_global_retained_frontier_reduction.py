from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from outline.semantic_final_fold_response import topic_fragment_inputs
from outline.semantic_identifier_aliases import WIRE_KEY as ALIAS_KEY
from outline.semantic_request_wire import expand_semantic_request_wire
from outline.v3_artifacts import OutlineArtifact
from outline.v3_executor import OutlineV3ExecutionError
from runtime.provider_runtime import hash_json
from services.artifact_registry import RegistryError
from services.job_workspace import publish_json_artifact
from tests import test_semantic_final_fold_runtime as fold
from tests.test_current_runtime_full_e2e import _source_closed_global_content
from tests.test_outline_v3_semantic_execution import _executor

NODE = "global_synthesis_provider"


def _rewrite(value, mapping):
    if isinstance(value, dict):
        return {key: _rewrite(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, mapping) for item in value]
    return mapping.get(value, value) if isinstance(value, str) else value


def _case(tmp_path, *, large=True, count=6, deferred_from=4, opaque=0, bridge_note="", deferred_assertion=False, distinct_sources=True):
    executor, seeds, _bases, source_request = fold._case(tmp_path)
    # Preserve this fixture's conservative retained-frontier reduction boundary.
    # The route-derived default can fit this synthetic case in one direct call,
    # bypassing the child-reduction and late-source mutation assertions below.
    executor.max_source_prompt_tokens = 32_000
    executor.review_intent_input["required_comparisons"] = [
        {"task_id": f"required:{index}", "question": f"Retain question {index}"} for index in range(4)
    ]
    scope_record = executor.artifact_records["topic_synthesis"]
    topics = json.loads(Path(scope_record.path).read_text(encoding="utf-8"))["payload"]["topics"]
    fragment = topics[0]["fragments"][0]
    topics[0]["fragments"] = [deepcopy(fragment) for _ in range(count)]
    identities = {*seeds[0]["source_claim_ids"], *seeds[0]["evidence_ids"],
                  *(row["source_field_id"] for row in seeds[0]["interpretation_context"]["fields"])}
    mappings = [{identity: (f"{identity}:partition:{index}" + (":opaque:" + "i" * opaque if opaque else "")) if distinct_sources else identity
                 for identity in identities} for index in range(count)]
    for index, item in enumerate(topics[0]["fragments"]):
        item["fragment_id"] = f"fragment:{index}"
        item["provider_results"][0].update(batch_id=f"batch:{index}", result_id=f"result:{index}")
        item["provider_results"] = _rewrite(item["provider_results"], mappings[index])
    artifact = executor._artifact(OutlineArtifact, {"topics": topics})
    record = publish_json_artifact(
        executor.publication_context, executor.registry, executor._node_path("global_source_topics"),
        artifact.to_dict(), artifact_role="outline_v3_node_output", artifact_type="outline_artifact",
        artifact_version="v3", producer="outline.v3_executor.OutlineV3Executor", artifact_id="test:global-source-topics",
        metadata={"job_id": executor.job_id, "node_id": "topic_synthesis", "content_hash": artifact.content_hash},
    )
    executor.artifact_records["topic_synthesis"] = record
    executor.artifact_paths["topic_synthesis"] = record.path
    bases = topic_fragment_inputs(topics)
    template_manifest = executor._load_semantic_reducer_coverage_ref(seeds[0])
    template_result = executor._load_semantic_reducer_result_ref(seeds[0])
    items = []
    for index, base in enumerate(bases):
        item = _rewrite(seeds[0], mappings[index])
        node = f"cross_group_comparison_provider:reduce:2:{index}"
        result = _rewrite(template_result, mappings[index])
        claim_id = f"synthesis:cross_group_comparison:leaf:{index}" + (":opaque:" + "i" * opaque if opaque else "")
        result["bridge_claims"][0]["claim_id"] = claim_id
        result["bridge_claims"][0]["text"] += bridge_note
        reason = f"Fragment {index} has no complete source-closed comparison."
        disposition = {"topic_id": item["topic_ids"][0], "status": "integrated", "synthesis_claim_ids": [claim_id]}
        if index >= deferred_from:
            result["bridge_claims"] = []
            disposition = {"topic_id": item["topic_ids"][0], "status": "deferred", "reason": reason}
        result["topic_dispositions"] = [disposition]
        result["unresolved_questions"] = [f"Retain uncertainty {index}"]
        # Complete source comparison prose is preserved in the parent, not
        # multiplied into each child. The deterministic fixture can synthesize
        # from its sealed bridge while leaving this original row unchanged.
        comparison = _rewrite(template_result["bridge_claims"][0], mappings[index])
        comparison.pop("claim_id")
        comparison.update(comparison_id=f"comparison:{index}", text=comparison["text"] + f" Source {index}: " + ("bounded comparison prose " * 1000 if large else "bounded comparison."))
        if index >= deferred_from and not deferred_assertion:
            # A source-grounded question has no assertion. Keep its position,
            # complete source pointers and qualifier closure under the normal
            # comparison validator, without presenting it as a finding.
            comparison.pop("text")
            comparison["question"] = f"Source {index}: how should these findings be compared under their supplied boundaries? " + ("Which recorded boundary needs further comparison? " * 500 if large else "")
        result["comparisons"] = [comparison]
        manifest = _rewrite(template_manifest, mappings[index])
        manifest.update(node_id=node, fragment_ids=[base["fragment_id"]], result_ids=base["result_ids"],
                        fragment_members_by_id={base["fragment_id"]: item["paper_ids"]},
                        source_item_hashes=[hash_json(base)], source_input_hash=hash_json([base]))
        coverage = executor._persist_semantic_reducer_input_manifest(node, manifest, dependency_hashes={})
        output = executor._persist_semantic_provider_output(node, result, dependency_hashes={})
        maps = {key: manifest[key] for key in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id")}
        item.update(reducer_id=node, semantic_result={key: value for key, value in result.items() if key != "topic_dispositions"},
                    topic_disposition_groups=[{"status": disposition["status"], "reason": disposition.get("reason", ""),
                                               "topic_ids": item["topic_ids"],
                                               "synthesis_claim_ids_by_topic": {item["topic_ids"][0]: [claim_id]} if index < deferred_from else {}}])
        item["coverage_ref"].update(artifact_id=coverage.artifact_id, registry_file_hash=coverage.content_hash,
                                    artifact_content_hash=coverage.metadata["content_hash"], node_id=node,
                                    manifest_hash=hash_json(manifest), membership_hash=hash_json(maps))
        item["semantic_result_ref"].update(artifact_id=output.artifact_id, registry_file_hash=output.content_hash,
                                           artifact_content_hash=output.metadata["content_hash"], provider_result_hash=hash_json(result))
        items.append(item)
    source_request["topic_synthesis"] = items
    fold._prepare(executor, items, bases, source_request)
    cross = executor._compile_final_fold_retention(fold.NODE, source_request, {})
    ledger = executor._build_semantic_coverage_ledger(bases, [], cross)
    parent = {"provider_output": cross, "coverage_ledger": ledger, "execution_mode": "source_closed_retention",
              "semantic_retention": {**deepcopy(executor._final_fold_results[fold.NODE]), "provider_invoked": False}}
    executor._payloads["cross_group_comparison"] = parent
    request = {"task": "substantive_global_synthesis", "node_id": "global_synthesis",
               "semantic_contract_version": "semantic-evidence-graph-v2", "shared_synthesis_contract_version": "v1",
               "topic_synthesis": [], "cross_group_comparison": cross, "questions": [fold.QUESTION],
               "topic_questions": [{"topic_id": topics[0]["topic_id"], "question": topics[0]["question"]}],
               "required_comparison_obligations": deepcopy(executor.review_intent_input["required_comparisons"]),
               "cross_coverage_ledger_ref": {"content_hash": ledger["content_hash"], "topic_count": len(ledger["topic_ids"])},
               "relation_candidates": [], "output_contract": {}}
    observed = []

    def provider(node, wire):
        # Exercise the production-like JSON boundary: object key order is not
        # preserved by transport, while arrays, primitive types and values are.
        wire = json.loads(json.dumps(wire, ensure_ascii=False, sort_keys=True))
        expanded = expand_semantic_request_wire(wire)
        observed.append((node, deepcopy(wire), deepcopy(expanded)))
        return {"status": "success", "content": _source_closed_global_content(expanded)}

    executor.provider = provider
    canonical = {}
    validator = executor._validate_semantic_provider_output

    def validate(node, full_request, output):
        validator(node, full_request, output)
        canonical[node] = deepcopy(full_request)

    executor._validate_semantic_provider_output = validate
    executor._test_canonical_requests = canonical
    return executor, request, parent, items, observed


def _run(executor, request):
    return executor._run_bounded_semantic_provider_call(NODE, request, {"cross_group_comparison": hash_json(request["cross_group_comparison"])})


def test_undercap_request_keeps_original_wire_hash_ids_and_response(tmp_path):
    executor, request, _parent, _items, observed = _case(tmp_path, large=False)
    expected = _source_closed_global_content(request)
    output = _run(executor, request)
    assert output == expected
    assert [node for node, *_ in observed] == [NODE]
    assert "global_retention_source" not in observed[0][2]
    receipt, = executor._receipt_ledger.list_receipts()
    assert receipt.input_hash == hash_json(observed[0][1])
    assert receipt.response_hash == hash_json(expected)


def test_overflow_uses_scoped_sealed_frontier_and_all_calls_fit_32k(tmp_path):
    executor, request, parent, items, observed = _case(tmp_path)
    before = hash_json(parent)
    output = _run(executor, request)
    assert output["synthesis_claims"] and output["organizing_principles"]
    assert len(observed) > 1
    profile = executor._node_route("candidate_1_provider_generation").profile
    deferred_calls = []
    for node, wire, expanded in observed:
        assert profile.estimate_tokens(wire) <= 32_000
        assert expanded["cross_group_comparison"] == {}
        assert expanded["questions"] == request["questions"]
        assert (expanded["topic_questions"][0]["question"] == request["topic_questions"][0]["question"])
        assert executor._test_canonical_requests[node]["topic_questions"] == request["topic_questions"]
        assert expanded["required_comparison_obligations"] == request["required_comparison_obligations"]
        assert len(expanded["required_comparison_obligations"]) == 4
        _records, reasons = executor._global_retention_member_scope(node, executor._test_canonical_requests[node])
        if reasons and ":reduce:" in node:
            deferred_calls.append(node)
            physical = executor.artifact_records[node]
            payload = json.loads(Path(physical.path).read_text(encoding="utf-8"))["payload"]
            assert payload["synthesis_claims"] == payload["organizing_principles"] == []
            assert set(reasons).issubset(payload["unresolved_questions"])
    assert hash_json(parent) == before
    first = [executor._test_canonical_requests[node] for node, _wire, _expanded in observed if node.startswith(f"{NODE}:reduce:1:")]
    assert sorted(hash_json(item) for child in first for item in child["topic_synthesis"]) == sorted(hash_json(item) for item in items)
    assert all(len(child["topic_synthesis"]) < len(items) for child in first)
    for child in first:
        source_manifests = [executor._load_semantic_reducer_coverage_ref(item) for item in child["topic_synthesis"]]
        node = next(node for node, full in executor._test_canonical_requests.items() if full == child)
        record = executor.artifact_records[f"{node}:input_manifest"]
        manifest = json.loads(Path(record.path).read_text(encoding="utf-8"))["payload"]
        for identity_field in ("fragment_ids", "result_ids"):
            assert set(manifest[identity_field]) == {value for source in source_manifests for value in source[identity_field]}
        assert set(manifest["source_field_ids"]) == {
            value for source in source_manifests for value in source["source_field_ids"]
        } | {
            field["source_field_id"] for item in child["topic_synthesis"]
            for field in (item.get("interpretation_context") or {}).get("fields") or []
        }
        for map_field in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id"):
            expected = {}
            for source in source_manifests:
                for identity, papers in source[map_field].items():
                    expected.setdefault(identity, set()).update(papers)
            assert {key: set(values) for key, values in manifest[map_field].items()} == expected
    assert deferred_calls
    assert NODE in executor._semantic_identifier_outputs
    state = executor._semantic_identifier_outputs[NODE]
    expanded_record = executor.registry.get(state["expanded_artifact_id"])
    executor.registry.verify_ready_artifact_closure(expanded_record)
    final_request = executor._test_canonical_requests[NODE]
    with pytest.raises(OutlineV3ExecutionError, match="substantive"):
        executor._validate_semantic_provider_output(NODE, final_request, {
            "synthesis_claims": [], "organizing_principles": [], "unresolved_questions": ["All evidence remains unresolved."],
        })


def test_exact_child_scope_rejects_foreign_parent_evidence_and_missing_question(tmp_path):
    executor, request, _parent, _items, observed = _case(tmp_path)
    _run(executor, request)
    node, _wire, _child = next(row for row in observed if ":reduce:" in row[0])
    child = deepcopy(executor._test_canonical_requests[node])
    forged = _source_closed_global_content(child)
    foreign = next(evidence for bridge in request["cross_group_comparison"]["bridge_claims"]
                   for evidence in bridge["evidence_ids"] if evidence not in json.dumps(child))
    assert foreign in json.dumps(request["cross_group_comparison"])
    forged["synthesis_claims"][0]["evidence_ids"] = [foreign]
    with pytest.raises(OutlineV3ExecutionError, match="unknown|source|evidence"):
        executor._validate_semantic_provider_output(node, child, forged)
    child["questions"] = []
    with pytest.raises(OutlineV3ExecutionError, match="question"):
        executor._semantic_request_contract(node, child)


def test_alias_child_canonical_reference_next_level_and_fresh_resume_zero_duplicate(tmp_path):
    executor, request, parent, _items, observed = _case(tmp_path, count=2, deferred_from=2, opaque=250)
    executor.max_source_prompt_tokens = 14_000
    result = _run(executor, request)
    executor._persist_semantic_provider_output(NODE, result, dependency_hashes={"cross_group_comparison": hash_json(request["cross_group_comparison"])})
    executor._register_receipt_ledger()
    aliases = [node for node, _wire, expanded in observed if ":reduce:" in node and ALIAS_KEY in expanded]
    assert aliases
    final_request = executor._test_canonical_requests[NODE]
    aliased_refs = [item for item in final_request["topic_synthesis"]
                    if (item.get("semantic_result_ref") or {}).get("response_kind") == "verified_identifier_expansion"]
    assert aliased_refs
    for item in aliased_refs:
        canonical = executor._load_semantic_reducer_result_ref(item)
        state = executor._semantic_identifier_outputs[item["reducer_id"]]
        assert state["physical_response_hash"] != hash_json(canonical)
        assert item["semantic_result_ref"]["provider_result_hash"] == hash_json(canonical)
    before = len(executor._receipt_ledger.list_receipts())
    fresh = _executor(tmp_path, stability_mode="off")
    fresh.max_source_prompt_tokens = 14_000
    fresh.review_intent_input = deepcopy(executor.review_intent_input)
    fresh._payloads["cross_group_comparison"] = deepcopy(parent)
    assert fresh.closure_epoch_id == executor.closure_epoch_id
    fresh._restore_local_final_fold(fold.NODE, parent)
    fresh.provider = lambda *_args: pytest.fail("fresh exact GLOBAL replay sent a duplicate physical call")
    restored = _run(fresh, request)
    assert hash_json(restored) == hash_json(result)
    assert len(fresh._receipt_ledger.list_receipts()) == before
    item = aliased_refs[0]
    state = executor._semantic_identifier_outputs[item["reducer_id"]]
    mapping = executor.registry.get(state["map_artifact_id"])
    executor.registry.update_record(mapping.artifact_id, depends_on=[
        dependency for dependency in mapping.depends_on
        if executor.registry.get(dependency.artifact_id).artifact_role != "outline_v3_semantic_reducer_input_manifest"
    ])
    fresh.registry.reload()
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        fresh._load_semantic_reducer_result_ref(item)
    assert len(fresh._receipt_ledger.list_receipts()) == before


def test_scoped_fixture_ids_do_not_collide_when_local_aliases_repeat():
    member = {"semantic_result": {"bridge_claims": [{
        "claim_id": "$I40", "text": "Source-bound statement.", "topic_ids": ["$I1"],
        "paper_keys": ["$I2"], "source_claim_ids": ["$I3"], "evidence_ids": ["$I4"],
    }]}}
    request = {"topic_synthesis": [member], "cross_group_comparison": {},
               "global_retention_source": {"source_request_hash": "a" * 64},
               "hierarchy": {"level": "bounded_semantic_reduction", "reduction_level": 1, "group_index": 1}}
    first = _source_closed_global_content(request)
    repeated = _source_closed_global_content(json.loads(json.dumps(request, sort_keys=True)))
    request["hierarchy"]["group_index"] = 2
    second = _source_closed_global_content(request)
    assert first == repeated
    assert first["synthesis_claims"][0]["claim_id"] != second["synthesis_claims"][0]["claim_id"]


@pytest.mark.parametrize("finding", ["A source-supported null finding.", "A source-supported reverse finding."])
def test_null_or_reverse_source_assertion_cannot_use_deferred_only_empty_output(tmp_path, finding):
    executor, request, _parent, _items, observed = _case(tmp_path, large=False, count=2, deferred_from=1, bridge_note=finding)
    marker, original, _records = executor._global_retention_authority(request)
    child = {**request, "cross_group_comparison": {}, "global_retention_source": marker,
             "topic_synthesis": original["topic_synthesis"][:1],
             "hierarchy": {"level": "bounded_semantic_reduction"}}
    _records, reasons = executor._global_retention_member_scope(f"{NODE}:reduce:1:1", child)
    assert reasons == []  # Nonempty support, irrespective of its effect direction.
    with pytest.raises(OutlineV3ExecutionError, match="substantive"):
        executor._validate_semantic_provider_output(f"{NODE}:reduce:1:1", child, {
            "synthesis_claims": [], "organizing_principles": [], "unresolved_questions": [finding],
        })
    assert observed == []


def test_comparison_only_supported_statement_cannot_use_empty_fallback(tmp_path):
    executor, request, parent, _items, observed = _case(tmp_path, large=False, count=2, deferred_from=1, deferred_assertion=True)
    before = hash_json(parent)
    marker, original, _records = executor._global_retention_authority(request)
    child = {**request, "cross_group_comparison": {}, "global_retention_source": marker,
             "topic_synthesis": original["topic_synthesis"][1:],
             "hierarchy": {"level": "bounded_semantic_reduction"}}
    assert not child["topic_synthesis"][0]["semantic_result"]["bridge_claims"]
    _records, reasons = executor._global_retention_member_scope(f"{NODE}:reduce:1:1", child)
    assert reasons == []
    with pytest.raises(OutlineV3ExecutionError, match="substantive"):
        executor._validate_semantic_provider_output(f"{NODE}:reduce:1:1", child, {
            "synthesis_claims": [], "organizing_principles": [], "unresolved_questions": ["Cannot silently discard a supported assertion."],
        })
    assert observed == [] and hash_json(parent) == before


@pytest.mark.parametrize("conflict", [True, False])
def test_new_global_child_claim_identity_conflicts_reject_before_next_call(tmp_path, conflict):
    executor, request, _parent, _items, observed = _case(
        tmp_path, large=False, count=2, deferred_from=2, distinct_sources=conflict,
    )
    marker, original, _records = executor._global_retention_authority(request)
    members = []
    retained = []
    for index, source in enumerate(original["topic_synthesis"]):
        node = f"{NODE}:reduce:1:{index + 1}"
        child = {**request, "cross_group_comparison": {}, "global_retention_source": marker,
                 "topic_synthesis": [source], "hierarchy": {"level": "bounded_semantic_reduction"}}
        scope_records, _reasons = executor._global_retention_member_scope(node, child)
        manifest = deepcopy(executor._load_semantic_reducer_coverage_ref(source))
        manifest.update(node_id=node, reduction_level=1, group_index=index + 1,
                        global_retention_source=marker, global_source_items=[source],
                        source_item_hashes=[hash_json(source)], source_input_hash=hash_json([source]),
                        child_input_manifest_hashes=[source["coverage_ref"]["registry_file_hash"]],
                        provider_request_hash=hash_json(child))
        coverage = executor._persist_semantic_reducer_input_manifest(
            node, manifest, dependency_hashes={}, source_records=scope_records,
        )
        result = _source_closed_global_content(child)
        result["synthesis_claims"][0]["claim_id"] = "synthesis:global_synthesis:shared-fixture-identity"
        result["synthesis_claims"][0]["source_claim_ids"] = [
            value for value in result["synthesis_claims"][0]["source_claim_ids"]
            if not value.startswith("synthesis:")
        ]
        executor._validate_semantic_provider_output(node, child, result)
        output = executor._persist_semantic_provider_output(node, result, dependency_hashes={}, recovery_sources=(coverage,))
        retained.extend([coverage, output])
        member = deepcopy(source)
        member.pop("topic_disposition_groups", None)
        member.update(reducer_id=node, semantic_result={key: value for key, value in result.items() if key not in {
            "processed_topic_ids", "processed_fragment_ids", "processed_result_ids", "processed_relation_ids",
        }})
        maps = {key: manifest[key] for key in ("topic_members_by_id", "fragment_members_by_id", "relation_members_by_id")}
        member["coverage_ref"].update(artifact_id=coverage.artifact_id, registry_file_hash=coverage.content_hash,
                                      artifact_content_hash=coverage.metadata["content_hash"], node_id=node,
                                      manifest_hash=hash_json(manifest), membership_hash=hash_json(maps))
        member["semantic_result_ref"].update(artifact_id=output.artifact_id, registry_file_hash=output.content_hash,
                                             artifact_content_hash=output.metadata["content_hash"], provider_result_hash=hash_json(result))
        members.append(member)
    before = {record.path: Path(record.path).read_bytes() for record in retained}
    final = {**request, "cross_group_comparison": {}, "global_retention_source": marker,
             "topic_synthesis": members, "hierarchy": {"level": "bounded_semantic_final_fold"}}
    if conflict:
        with pytest.raises(OutlineV3ExecutionError, match="conflicting canonical definitions"):
            executor._run_bounded_semantic_provider_call(NODE, final, {})
    else:
        executor._global_retention_member_scope(NODE, final)
        assert members[0]["semantic_result"]["synthesis_claims"] == members[1]["semantic_result"]["synthesis_claims"]
    assert observed == [] and executor._receipt_ledger.list_receipts() == ()
    assert all(Path(path).read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("before,after", [(False, 0), (1, 1.0)])
def test_same_revision_used_record_numeric_type_change_is_rejected(tmp_path, monkeypatch, before, after):
    executor, request, _parent, _items, observed = _case(tmp_path, large=False, count=2, deferred_from=1)
    marker, original, _records = executor._global_retention_authority(request)
    scoped = {**request, "cross_group_comparison": {}, "global_retention_source": marker,
              "topic_synthesis": original["topic_synthesis"], "hierarchy": {"level": "bounded_semantic_final_fold"}}
    target_id = original["topic_synthesis"][0]["coverage_ref"]["artifact_id"]
    path = Path(executor.registry.registry_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    row = next(row for row in data["artifacts"] if row["artifact_id"] == target_id)
    row["metadata"]["numeric_signature_control"] = before
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    executor.registry.reload()
    revision, _snapshot = executor.registry.capture_record_snapshot()
    loader = executor._load_semantic_reducer_result_ref
    changed = False

    def race(value, **kwargs):
        nonlocal changed
        result = loader(value, **kwargs)
        if not changed:
            path = Path(executor.registry.registry_path)
            data = json.loads(path.read_text(encoding="utf-8"))
            row = next(row for row in data["artifacts"] if row["artifact_id"] == target_id)
            assert type(row["metadata"]["numeric_signature_control"]) is type(before)
            row["metadata"]["numeric_signature_control"] = after
            path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            changed = True
        return result

    monkeypatch.setattr(executor, "_load_semantic_reducer_result_ref", race)
    with pytest.raises((OutlineV3ExecutionError, RegistryError), match="snapshot|Registry|changed"):
        executor._global_retention_member_scope(NODE, scoped)
    final_revision, _snapshot = executor.registry.capture_record_snapshot()
    assert final_revision == revision and changed and observed == []


@pytest.mark.parametrize("mutation", ["bytes", "record_map"])
def test_source_late_mutation_during_full_semantic_validation_rejects(tmp_path, mutation):
    executor, request, _parent, _items, _observed = _case(tmp_path)
    output = _run(executor, request)
    final_request = executor._test_canonical_requests[NODE]
    target = executor.registry.get(final_request["topic_synthesis"][0]["coverage_ref"]["artifact_id"])

    class LateChange(dict):
        def items(self):
            if mutation == "bytes":
                Path(target.path).write_text("{}", encoding="utf-8")
            else:
                executor.registry.update_record(target.artifact_id, depends_on=[])
            return super().items()

    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        executor._validate_semantic_provider_output(NODE, final_request, LateChange(output))


def test_insufficient_existing_call_budget_rejects_before_first_transport(tmp_path):
    executor, request, _parent, _items, observed = _case(tmp_path)
    executor.max_provider_calls = 0
    with pytest.raises(OutlineV3ExecutionError, match="budget"):
        _run(executor, request)
    assert observed == []


@pytest.mark.parametrize("mutation", ["request_bytes", "catalog_bytes", "member_bytes", "member_hash", "duplicate_member"])
def test_retained_frontier_source_tamper_fails_closed_without_transport(tmp_path, mutation):
    executor, request, _parent, items, observed = _case(tmp_path)
    if mutation in {"request_bytes", "catalog_bytes", "member_bytes"}:
        artifact_id = (executor._final_fold_results[fold.NODE]["request_artifact_id"] if mutation == "request_bytes" else
                       executor._final_fold_results[fold.NODE]["catalog_artifact_id"] if mutation == "catalog_bytes" else
                       items[0]["semantic_result_ref"]["artifact_id"])
        Path(executor.registry.get(artifact_id).path).write_text("{}", encoding="utf-8")
    else:
        marker, source, _records = executor._global_retention_authority(request)
        scoped = {**request, "cross_group_comparison": {}, "global_retention_source": marker,
                  "topic_synthesis": deepcopy(source["topic_synthesis"]),
                  "hierarchy": {"level": "bounded_semantic_final_fold"}}
        if mutation == "member_hash":
            scoped["topic_synthesis"][0]["semantic_result_ref"]["provider_result_hash"] = "0" * 64
        else:
            scoped["topic_synthesis"].append(deepcopy(scoped["topic_synthesis"][0]))
        request = scoped
    with pytest.raises((OutlineV3ExecutionError, RegistryError)):
        _run(executor, request)
    assert observed == []
