# 运行时真相源与契约

本文列出当前运行时使用的持久化事实。未列为规范真相源的文件只能作为
投影、导出、缓存或诊断，不能满足 readiness 或 completion gate。

## Job 与来源身份

- `source_inventory_v1.json` 是 Zotero 报告、PDF、显式 summary 和分类文件
  的内容哈希来源身份真相。
- 没有 DOI 时，规范来源身份必须同时满足规范化标题匹配，以及真实首作者
  或年份匹配。只有标题的观察必须 quarantine。
- `artifact_registry.json` 是 artifact/dependency 图。Registry 写入使用
  workspace lease、revisioned transaction、原子替换和损坏时 fail-closed。
- `job_outcome_v1.json` 是 job head 投影，记录生命周期、disposition、
  readiness policy、必需/完成阶段和 `canonical_ready`。
- `artifacts/job_attempts/snapshot-*.json` 是 append-only attempt history。
  过期的 running attempt 变为 `interrupted`，不会被下一次运行改写。
- `runtime_stage_terminals/*/*.json` 只有在 output、hash、schema、dependency
  和 terminal record 全部通过校验时，才证明阶段完成。

Queue 生命周期只读取 `job_status`；人类可读的 success 标志不是真相源。

## 各阶段规范真相源

| 阶段 | 规范真相源 | 投影/导出 |
|---|---|---|
| Source intake | `source_inventory_v1.json`、`source_bundle.json` | parser 诊断和只读 paper view |
| Stage 1 | 规范 `*_summaries.json`、已注册 `paper_artifacts/*.json`、evidence manifest 和来源链 | Excel 与显示用 summary |
| Outline Intelligence v3 | 已注册 evidence views、corpus ledger、multi-view matrix、review intent、coverage contract、relation map、candidate plan、node DAG、receipts、final outline、stage health 和 adoption record | Markdown 或人类可读 outline 展示 |
| Review | `review_draft.json`（`artifact_version=v3`）、`citation_manifest_v3.json` 和 citation-reference catalog | DOCX 与文本报告 |
| Validation | `validation_run_result_v1.json` 及其对 review draft、citation manifest、evidence manifest 的精确 Registry `depends_on` 闭包 | TXT、manual-review JSON、alignment audit 和 completion projection |
| Repair | 与 validation-run artifact 绑定的已注册 repair plan 和 apply result | 人类可读 repair 摘要 |

## 公开状态

`job_status` 为 `pending | running | completed | failed | cancelled`。

`job_disposition` 为 `clean | findings | needs_review | unvalidated`。

`claim_verdict` 为 `supported | partial_support | evidence_gap | unsupported |
contradicted | wrong_source | needs_review`。

缺少证据只能得到 `evidence_gap`，不会自动变成 `unsupported`。身份
`ambiguous/mismatch` 时可以完成诊断，但必须 quarantine，并保持
`canonical_ready=false`。

零 claim 的 Validation 只有在 review 明确声明 citation-free 时才可 clean。
成功 Validation 必须在规范 JSON 回读后确认 job ID、attempt ID 和 content hash。
规范 payload 的 artifact ID/type/hash 多重集合必须与 Registry `depends_on`
完全一致；缺失、额外、重复、类型错误、job-kind 错误、路径错误或 hash 错误
均 fail-closed。

## 派生 review batch

`SummarySelectionSpecV1` 固定 parent job、parent artifact ID/hash、有序 paper
key、可选分类文件 hash、selection policy 和 selection hash。child artifact
使用 `external_job` 依赖，不能跨越 Stage 1 provider 边界。

每次派生在写 child 或 manifest 前预留持久化单调递增的
`projection_generation`。lease 串行化所有权与发布；经过完整校验且 generation
唯一最大的 Registry manifest 是 coordinator head，人类可读 manifest 只是可修复投影。

## AI-native 运行时

`RuntimeJobSpec` 与 `AgentRuntimeRunner` 在内部 `AgentRuntimeBridge` 之上提供
公开执行契约：

- `run`：新 job 与 attempt；
- `resume`：新 append-only attempt，只复用已证明持久化的阶段；
- `status`：只读 job head；
- `reconcile`：无 provider 调用地修复 Registry、pointer 和 terminal 投影。

每次 provider 调用都通过 typed context profile 绑定 job、attempt、stage 和
node，并生成去除敏感信息的 receipt，记录 request identity、retry/timeout、
response hash 和 completion-evaluator 结果。相对路径从所属 spec、config 或
summary 文件解析；reconcile 不调用 provider。

## Outline v3 与控制面

Outline Intelligence v3 是确定性的已注册 DAG。node output 绑定来源 summary
hash，保留 replay receipt，并且 resume 只重跑失败节点的依赖闭包。final outline
只有在 coverage、stage-health、identity 和 canonical-completion gate 全部通过后
才可 adoption。

`reviewctl` 是唯一控制面。`status`、`next-action`、`validate`、`inspect`、
`attest` 是无 provider 的读取；`run`、`resume`、`retry-node`、`cancel`、
`repair-plan`、`repair-apply`、`adopt`、`export` 是显式的 Registry-backed 状态
迁移。cancel 是 cooperative 的，被取消 job 不得发布为 completed。

Validation closure 要求当前 review draft、citation manifest 与
`ValidationRunResultV1` 的输入 ID/hash 一致。Repair 默认 `report_only`；显式
安全事务只创建 quarantine 的派生产物。Adoption 不会静默提升中间 candidate。

Export bundle 包含已验证文件、provenance、checksum、completion evidence 和
validation-closure evidence。`canonical_verified`、`manual_repaired`、
`untrusted` 是 attestation 标签，不是 job 成功别名；只有 DOCX 不能证明完成。
