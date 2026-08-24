# Prompt Inventory

`prompts/registry.json` is the authority for prompt identity, status, owner,
placeholder declarations, output contract, version, and SHA-256. Production
callers load ACTIVE prompts through `services.prompt_registry.PromptRegistry`.
Changing a prompt file without updating its declared hash fails closed.

| prompt_id | path | owner / callsite | status | placeholders | output contract | version | receipt / replay / reuse binding |
|---|---|---|---|---|---|---|---|
| `stage1.analysis.system.v3` | `prompts/active/stage1/system_analysis_v3.txt` | `Stage1AnalysisService` -> reader system prompt | ACTIVE | none | `summary_v2_lite` JSON | v3 | Stage 1 prompt authority, provider receipt metadata, expected call graph, reuse binding |
| `stage1.analysis.user.v3` | `prompts/active/stage1/user_analysis_v3.txt` | `Stage1AnalysisService` -> `Stage1InputBuilder` | ACTIVE | `PAPER_FULL_TEXT`, `VISUAL_COVERAGE_JSON`, `SUMMARY_SCHEMA_CONTRACT` | `summary_v2_lite` JSON | v3 | Stage 1 prompt authority, provider receipt metadata, expected call graph, reuse binding |
| `stage1.visual_scan.system.v2` | `prompts/active/stage1/system_visual_scan_v2.txt` | `stage1_visual_scan.build_visual_scan_prompt` | ACTIVE | none | `stage1_visual_observations/v2` JSON with page-to-child attribution | v2 | Visual scan call identity, candidate metadata, observation artifact, receipt, expected-call graph, and reuse schema binding |
| `free_mode.chat.system.v1` | `prompts/active/free_mode/system_chat_v1.txt` | `free_mode.service.plan_free_mode_chat_turn` | ACTIVE | none | Free Mode planner JSON | v1 | Free Mode provider receipt metadata |
| `free_mode.profile.system.v1` | `prompts/active/free_mode/system_profile_v1.txt` | `free_mode.service.generate_free_mode_profile` | ACTIVE | none | Free Mode profile JSON | v1 | Free Mode provider receipt metadata |
| `outline.node.system.v3` | `prompts/active/outline/system_outline_node_v3.txt` | `OutlineV3Executor` provider node binding | ACTIVE | none | Outline v3 node JSON | v3 | Outline provider binding, receipt, replay key |
| `outline.node.policies.v3` | `prompts/active/outline/node_policies_v3.json` | `OutlineV3Executor` node-policy input | ACTIVE | none | node-role policy map | v3 | Outline node input/replay identity |
| `review.section_writer.system.v3` | `prompts/active/review/system_section_writer_v3.txt` | `ReviewGenerationService._system_prompt` | ACTIVE | none | non-empty JSON `blocks` with `[[cite_ref:R###]]` tokens | v3 | Writer binding, receipt, section replay |
| `validation.adjudicator.system.v2` | `prompts/active/validation/system_adjudicator_v2.txt` | `validation.llm_adjudicator.run_adjudication_stage` | ACTIVE | none | validation adjudication JSON | v2 | Validator receipt and adjudication reuse record |
| `validation.adjudicator.user.v2` | `prompts/active/validation/user_adjudicator_v2.txt` | `validation.llm_adjudicator.run_adjudication_stage` | ACTIVE | `ADJUDICATION_STAGE`, `ADJUDICATION_PACKET_JSON` | validation adjudication JSON | v2 | Validator receipt, replay/reuse key, adjudication reuse record |
| `validation.repair_rewrite.system.v1` | `prompts/active/validation/system_repair_rewrite_v1.txt` | `validator._rewrite_block_with_ai` | ACTIVE | none | rewritten claim-unit JSON | v1 | Validator repair provider binding |
| `validation.legacy.summary_fact_check.v1` | `prompts/legacy/prompt_validate_analysis_strict.txt` | compatibility `validator.validate_summary_with_ai` | LEGACY | `PAPER_FULL_TEXT`, `GENERATED_SUMMARY` | legacy correction JSON | v1 | Legacy compatibility call only; not an ACTIVE default |
| `validation.legacy.claims_batch.v1` | `prompts/legacy/prompt_validate_claims_batch.txt` | compatibility `validator._validate_claims_for_single_paper` | LEGACY | `SOURCE_SUMMARY`, `SENTENCES_TO_VALIDATE` | legacy batch validation JSON | v1 | Legacy compatibility call only; not an ACTIVE default |
| `validation.legacy.summary_fact_check.system.v1` | `prompts/legacy/system_summary_fact_check_v1.txt` | compatibility `validator.validate_summary_with_ai` | LEGACY | none | legacy correction JSON | v1 | Legacy compatibility call only |
| `validation.legacy.claims_batch.system.v1` | `prompts/legacy/system_claims_batch_v1.txt` | compatibility `validator._validate_claims_for_single_paper` | LEGACY | none | legacy batch validation JSON | v1 | Legacy compatibility call only |

The former `stage1.visual_scan.system.v1` prompt is retained only as
`prompts/legacy/stage1/system_visual_scan_v1.txt`; it is not an ACTIVE
production route and v1 observation artifacts cannot satisfy the current v2
reuse qualification.

The deleted files under the former flat `prompts/` directory had no current
production caller after this audit. They are not copied into `legacy/`.

## Hashes

The exact SHA-256 values are intentionally kept in `prompts/registry.json` so
code, tests, receipts, and this inventory have one machine-readable authority.
The registry validation test checks every ACTIVE file, placeholder declaration,
owner, version, and orphan-file condition.
