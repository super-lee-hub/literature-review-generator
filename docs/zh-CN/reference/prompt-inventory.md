# Prompt 清单

`prompts/registry.json` 是 Prompt 身份、状态、owner、占位符、输出契约、版本和
SHA-256 的唯一真源。生产调用只能通过
`services.prompt_registry.PromptRegistry` 加载 ACTIVE Prompt。文件内容变化而
未同步声明 hash 时，加载会 fail closed。

| prompt_id | 路径 | owner / 调用点 | 状态 | 占位符 | 输出契约 | 版本 | receipt / replay / reuse 绑定 |
|---|---|---|---|---|---|---|---|
| `stage1.analysis.system.v3` | `prompts/active/stage1/system_analysis_v3.txt` | `Stage1AnalysisService` -> reader system prompt | ACTIVE | 无 | `summary_v2_lite` JSON | v3 | Stage 1 Prompt authority、provider receipt、expected call graph、reuse binding |
| `stage1.analysis.user.v3` | `prompts/active/stage1/user_analysis_v3.txt` | `Stage1AnalysisService` -> `Stage1InputBuilder` | ACTIVE | `PAPER_FULL_TEXT`、`VISUAL_COVERAGE_JSON`、`SUMMARY_SCHEMA_CONTRACT` | `summary_v2_lite` JSON | v3 | Stage 1 Prompt authority、provider receipt、expected call graph、reuse binding |
| `stage1.visual_scan.system.v2` | `prompts/active/stage1/system_visual_scan_v2.txt` | `stage1_visual_scan.build_visual_scan_prompt` | ACTIVE | 无 | 带页到 child 归因的 `stage1_visual_observations/v2` JSON | v2 | visual scan call identity、候选元数据、observation artifact、receipt、expected-call graph 和 reuse schema binding |
| `free_mode.chat.system.v1` | `prompts/active/free_mode/system_chat_v1.txt` | `free_mode.service.plan_free_mode_chat_turn` | ACTIVE | 无 | Free Mode planner JSON | v1 | Free Mode provider receipt metadata |
| `free_mode.profile.system.v1` | `prompts/active/free_mode/system_profile_v1.txt` | `free_mode.service.generate_free_mode_profile` | ACTIVE | 无 | Free Mode profile JSON | v1 | Free Mode provider receipt metadata |
| `outline.node.system.v3` | `prompts/active/outline/system_outline_node_v3.txt` | `OutlineV3Executor` provider node binding | ACTIVE | 无 | Outline v3 node JSON | v3 | Outline provider binding、receipt、replay key |
| `outline.node.policies.v3` | `prompts/active/outline/node_policies_v3.json` | `OutlineV3Executor` node-policy 输入 | ACTIVE | 无 | node-role policy map | v3 | Outline node input/replay identity |
| `review.section_writer.system.v3` | `prompts/active/review/system_section_writer_v3.txt` | `ReviewGenerationService._system_prompt` | ACTIVE | 无 | 非空 JSON `blocks` 和 `[[cite_ref:R###]]` token | v3 | Writer binding、receipt、section replay |
| `validation.adjudicator.system.v2` | `prompts/active/validation/system_adjudicator_v2.txt` | `validation.llm_adjudicator.run_adjudication_stage` | ACTIVE | 无 | validation adjudication JSON | v2 | Validator receipt 和 adjudication reuse record |
| `validation.adjudicator.user.v2` | `prompts/active/validation/user_adjudicator_v2.txt` | `validation.llm_adjudicator.run_adjudication_stage` | ACTIVE | `ADJUDICATION_STAGE`、`ADJUDICATION_PACKET_JSON` | validation adjudication JSON | v2 | Validator receipt、replay/reuse key、adjudication reuse record |
| `validation.repair_rewrite.system.v1` | `prompts/active/validation/system_repair_rewrite_v1.txt` | `validator._rewrite_block_with_ai` | ACTIVE | 无 | rewritten claim-unit JSON | v1 | Validator repair provider binding |
| `validation.legacy.summary_fact_check.v1` | `prompts/legacy/prompt_validate_analysis_strict.txt` | 兼容入口 `validator.validate_summary_with_ai` | LEGACY | `PAPER_FULL_TEXT`、`GENERATED_SUMMARY` | legacy correction JSON | v1 | 仅兼容调用，不是 ACTIVE 默认路径 |
| `validation.legacy.claims_batch.v1` | `prompts/legacy/prompt_validate_claims_batch.txt` | 兼容入口 `validator._validate_claims_for_single_paper` | LEGACY | `SOURCE_SUMMARY`、`SENTENCES_TO_VALIDATE` | legacy batch validation JSON | v1 | 仅兼容调用，不是 ACTIVE 默认路径 |
| `validation.legacy.summary_fact_check.system.v1` | `prompts/legacy/system_summary_fact_check_v1.txt` | 兼容入口 `validator.validate_summary_with_ai` | LEGACY | 无 | legacy correction JSON | v1 | 仅兼容调用 |
| `validation.legacy.claims_batch.system.v1` | `prompts/legacy/system_claims_batch_v1.txt` | 兼容入口 `validator._validate_claims_for_single_paper` | LEGACY | 无 | legacy batch validation JSON | v1 | 仅兼容调用 |

旧的 `stage1.visual_scan.system.v1` 只保留在
`prompts/legacy/stage1/system_visual_scan_v1.txt`，不再是 ACTIVE 生产路由；v1
observation artifact 不能满足当前 v2 reuse qualification。

审计后没有当前生产调用者的旧 flat prompt 已删除，不复制到 `legacy/`。所有
精确 hash 保存在 `prompts/registry.json`，测试会校验 ACTIVE 文件、占位符、owner、
版本和 orphan 文件。
