# 运行时真相源与数据契约

本文列出当前运行时的规范持久化事实。未列为规范真相源的文件，只能视为投影、导出、缓存或兼容输入。

## Job 与来源身份

- `source_inventory_v1.json`：Zotero 报告、PDF、显式 summary 和分类文件的内容级来源身份真相。
- `artifact_registry.json` v2：artifact 与依赖图；采用工作区写锁、revision 事务、原子替换和损坏时 fail-closed。
- `job_outcome_v1.json`：当前 job head，记录生命周期、disposition、readiness policy、必需/完成阶段与 `canonical_ready`。
- `artifacts/job_attempts/snapshot-*.json`：append-only attempt 历史；陈旧 running attempt 终结为 `interrupted`，不会被下一次恢复改写。
- `runtime_stage_terminals/*/*.json`：只有文件、Registry、hash、schema、依赖和终态记录全部有效时，才能证明阶段完成。

旧 `success` 仅投影 `canonical_ready`；Queue 生命周期只读取 `job_status`。

## 各阶段规范真相源

| 阶段 | 规范真相源 | 投影/导出 |
|---|---|---|
| 来源接入 | `source_inventory_v1.json`、`source_bundle.json` | 旧 `List[PaperInfo]` |
| Stage 1 | 规范 `*_summaries.json`、已注册 `paper_artifacts/*.json`、evidence manifest | Excel 与旧 summary 结构 |
| Outline v2 | literature map、synthesis flow、candidates、critiques、arbitration、`final_outline`、coverage audit，以及独立 `outline_stage_health_v1.json`；v2 开启时下游只消费已注册 `adopted_final_outline` | 仅在显式关闭 v2 时使用旧 Markdown outline |
| Review | `*_review_draft_v2.json`、`*_citation_manifest_v3.json`、citation-ref catalog | review draft v1 与 DOCX |
| Validation | `validation_run_result_v1.json`（`ValidationRunResultV1`） | 从规范 JSON 投影的 TXT、manual-review、alignment audit 和 completion report |
| Repair | 与 validation-run artifact 绑定的 repair plan 与 apply result | 人类可读修复摘要 |

`claim_verdict` 为：`supported | partial_support | evidence_gap | unsupported | contradicted | wrong_source | needs_review`。没有足够证据只能是 `evidence_gap`，不能自动写成 `unsupported`。

身份为 `ambiguous/mismatch` 时，job 可以完成诊断，但必须 quarantine，且 `canonical_ready=false`。

## 派生综述与 AI-native 运行时

`SummarySelectionSpecV1` 固定父 job、父 artifact ID/hash、有序 paper keys、分类文件 hash、选择策略和 selection hash；child 使用 `external_job` 依赖，禁止调用 Stage 1 provider。

`AgentRuntimeRunner` 复用现有 `AgentRuntimeBridge`：`run` 启动新任务，`resume` 创建新 attempt，`status` 只读，`reconcile` 只修复持久化投影且绝不调用 provider。相对路径按其所属 spec/config/summary 文件目录解析，不静默回退 CWD。
