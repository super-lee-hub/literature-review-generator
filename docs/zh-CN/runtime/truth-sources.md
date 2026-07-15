# 运行时真相源与数据契约

本文列出当前运行时的规范持久化事实。未列为规范真相源的文件，只能视为投影、导出、缓存或兼容输入。

## Job 与来源身份

- `source_inventory_v1.json`：Zotero 报告、PDF、显式 summary 和分类文件的内容级来源身份真相。
- 没有 DOI 时，规范来源身份必须同时满足规范化标题匹配，以及真实首作者或年份至少一项匹配；只有标题证据时仍须 quarantine。
- `artifact_registry.json` v2：artifact 与依赖图；采用工作区写锁、revision 事务、原子替换和损坏时 fail-closed。每个 READY 依赖在提交前都必须核对持久化 Registry 身份、状态、路径和内容 hash。
- `job_outcome_v1.json`：当前 job head，记录生命周期、disposition、readiness policy、必需/完成阶段与 `canonical_ready`。
- `artifacts/job_attempts/snapshot-*.json`：append-only attempt 历史；陈旧 running attempt 终结为 `interrupted`，不会被下一次恢复改写。
- `runtime_stage_terminals/*/*.json`：只有文件、Registry、hash、schema、依赖和终态记录全部有效时，才能证明阶段完成。

旧 `success` 仅投影 `canonical_ready`；Queue 生命周期只读取 `job_status`。

## 各阶段规范真相源

| 阶段 | 规范真相源 | 投影/导出 |
|---|---|---|
| 来源接入 | `source_inventory_v1.json`、`source_bundle.json` | 旧 `List[PaperInfo]` |
| Stage 1 | 规范 `*_summaries.json`、已注册 `paper_artifacts/*.json`、evidence manifest；READY summary 依赖已注册 `source_bundle`，后者再依赖来源 PDF | Excel 与旧 summary 结构 |
| Outline v2 | literature map、synthesis flow、candidates、critiques、arbitration、`final_outline`、coverage audit，以及独立 `outline_stage_health_v1.json`；v2 开启时下游只消费已注册 `adopted_final_outline` | 仅在显式关闭 v2 时使用旧 Markdown outline |
| Review | `*_review_draft_v2.json`、`*_citation_manifest_v3.json`、citation-ref catalog | review draft v1 与 DOCX |
| Validation | `validation_run_result_v1.json`（`ValidationRunResultV1`）及其精确 Registry `depends_on` 闭包：review draft、citation manifest、全部已声明 evidence manifest | 从规范 JSON 投影的 TXT、manual-review、alignment audit 和 completion report |
| Repair | 与 validation-run artifact 绑定的 repair plan 与 apply result | 人类可读修复摘要 |

`claim_verdict` 为：`supported | partial_support | evidence_gap | unsupported | contradicted | wrong_source | needs_review`。没有足够证据只能是 `evidence_gap`，不能自动写成 `unsupported`。

身份为 `ambiguous/mismatch` 时，job 可以完成诊断，但必须 quarantine，且 `canonical_ready=false`。

零 claim 的 Validation 结果只有在综述被显式声明为 citation-free 时才能为 clean；否则 claim completeness 为 false，不能发布 clean disposition。Validation 成功还必须在规范 JSON 回读后确认 job ID、attempt ID 与内容 hash 一致。

review、citation 与 evidence 输入 hash 均必须是 64 位小写 SHA-256。规范 payload 中声明的 artifact ID/type/hash 多重集合必须与 Registry `depends_on` 多重集合完全一致；缺失、额外、重复、类型错误、job-kind 错误、路径错误或 hash 错误都必须 fail closed。ReviewBatch child 可以继续使用 `external_job` evidence 依赖，但必须唯一解析并递归校验。evidence manifest 被删除、篡改、quarantine 或标记 invalid 后，Validation terminal 立即失效，`resume` 不得复用该 Validation 结果。

## 派生综述与 AI-native 运行时

`SummarySelectionSpecV1` 固定父 job、父 artifact ID/hash、有序 paper keys、分类文件 hash、选择策略和 selection hash；child 使用 `external_job` 依赖，禁止调用 Stage 1 provider。

每个多 variant derivation 在 child 或 manifest 写入前持久化预留单调 `projection_generation`。per-derivation lease 与 coordinator lease 串行化所有权和 projection 发布。Registry 中通过完整校验、且 generation 唯一最大的 immutable manifest 是 coordinator head；`review_batch_manifest.json` 只是该 head 的可修复投影。`review-batch-projection-generation-v1` reservation 与 `review-batch-projection-receipt-v2` receipt 记录持久化顺序，以及 `projected` 或 `superseded` 的 head 身份、generation 和 hash；排序不依赖 mtime 或系统时钟。

`AgentRuntimeRunner` 复用现有 `AgentRuntimeBridge`：`run` 启动新任务，`resume` 创建新 attempt，`status` 只读，`reconcile` 只修复持久化投影且绝不调用 provider。相对路径按其所属 spec/config/summary 文件目录解析，不静默回退 CWD。

`SystemExit` 与其他 `BaseException` 路径会先持久化终态，再原样抛出异常。resume 从规范 terminal artifact 恢复 Validation disposition，不解析人类可读投影。
