"""The expected validation identity must equal the transport identity."""
from __future__ import annotations

from types import SimpleNamespace

import validation.llm_adjudicator as adjudicator
from runtime.provider_runtime import canonical_provider_request_payload
from validation.llm_adjudicator import AdjudicationPacket


def test_validation_expected_input_matches_actual_provider_serializer(monkeypatch) -> None:
    observed = {}

    class Service:
        logger = None

        def new_provider_runtime(self, **_kwargs):
            return SimpleNamespace(
                call_id="validation:primary:fixture",
                schema_hash="fixture-schema",
                receipts=["fixture receipt"],
            )

        def bind_provider_call(self, **kwargs):
            observed["expected"] = kwargs

        def bind_provider_output(self, **_kwargs):
            pass

    def fixture_transport(prompt, api_config, system_prompt, **kwargs):
        del api_config
        observed["actual_prompt"] = prompt
        observed["actual_system"] = system_prompt
        observed["transport_options"] = kwargs
        return {"status": "supported", "confidence": 0.9}

    monkeypatch.setattr(adjudicator, "_call_ai_api", fixture_transport)
    packet = AdjudicationPacket(
        citation_set_key="fixture-citation",
        stage="primary",
        claim_text="The result is supported by the cited source.",
        claim_context="",
        block_context="",
        claim_type="result",
        claim_type_confidence=0.9,
        claim_type_rationale="explicit result",
        paper_ids=["paper-a"],
        claim_units=[],
        target_claim_unit={},
        claim_unit_results=[],
        paper_identity_hints={},
        per_paper_evidence_packets={"paper-a": {}},
        evidence_excerpt_list=[],
        trimmed_candidate_counts={},
        evidence_status="supported",
        disposition="keep_as_is",
    )
    result = adjudicator.run_adjudication_stage(
        Service(), {"model": "local-fixture", "max_output_tokens": 2048}, packet
    )
    assert result is not None
    options = observed["transport_options"]
    assert observed["expected"]["input_payload"] == canonical_provider_request_payload(
        prompt=observed["actual_prompt"],
        system_prompt=observed["actual_system"],
        user_content=None,
        response_format=options["response_format"],
        max_output_tokens=options["max_tokens"],
        temperature=options["temperature"],
    )
