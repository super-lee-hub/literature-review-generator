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
- Queue-owned canonical bytes 必须经过带 lease generation 的 staging context。
  发布边界先取得 queue store lock，再复核 lease/worker/generation/fence，随后
  进入 Registry transaction；禁止反向锁顺序。成功的字节发布还会写入不可变的
  `lease_publication_manifest`；若 Registry 注册失败，immutable orphan 保留为
  诊断证据，不恢复可变 fixed target。
- `job_outcome_v1.json` 是 job head 投影，记录生命周期、disposition、
  readiness policy、必需/完成阶段和 `canonical_ready`。
- `artifacts/job_attempts/snapshot-*.json` 是 append-only attempt history。
  过期的 running attempt 变为 `interrupted`，不会被下一次运行改写。
- `runtime_stage_terminals/*/*.json` 只有在 output、hash、schema、dependency
  和 terminal record 全部通过校验时，才证明阶段完成。
- `current-artifact-set:pointer` 解析出一个不可变的
  `CurrentArtifactSetV1`，其中包含五个精确的当前目标（draft、citation
  manifest、DOCX、validation result、validation receipt closure）及其 hash。
  pointer 通过 compare-and-swap 推进；未被该 set 引用的旧 READY 产物只保留
  为历史或 quarantine 状态。

`artifacts/runtime_job_spec_v1.json` 同时保存规范 `StagePlan`。`run_all` 在
validation 启用时固定请求 `analyze`、`outline`、`review`、`validate`；只有在
明确把 validation 设为 optional 且禁用时才省略 `validate`。两条路径都仍然
要求 current artifact set 才能 canonical-ready。派生任务和 outline-only 任务
在没有 current set 时不能 canonical-ready；中间 outline candidate 必须显式
adoption 后才能进入 review 路径。

Queue 生命周期只读取 `job_status`；人类可读的 success 标志不是真相源。

## 各阶段规范真相源

| 阶段 | 规范真相源 | 投影/导出 |
|---|---|---|
| Source intake | `source_inventory_v1.json`、`source_bundle.json` | parser 诊断和只读 paper view |
| Stage 1 | 不可变 content-addressed 规范 `*_summaries.json`、已注册 `paper_artifacts/*.json`、evidence manifest、来源链、expected-call closure、reuse record 和当前 epoch receipt ledger | Excel 与显示用 summary |
| Outline Intelligence v3 | 已注册 evidence views、corpus ledger、multi-view matrix、review intent、coverage contract、relation map、candidate plan、node DAG、receipts、final outline、stage health 和 adoption record | Markdown 或人类可读 outline 展示 |
| Review | `review_draft.json`（`artifact_version=v3`）、`citation_manifest_v3.json` 和 citation-reference catalog，并通过 current artifact set 解析 | DOCX 与文本报告 |
| Validation | `validation_run_result_v1.json` 及其对 review draft、citation manifest、evidence manifest 的精确 Registry `depends_on` 闭包；optional validation 禁用时用 typed `ValidationDispositionV1(status=not_requested, allow_unvalidated=true)` 和空 receipt closure 绑定 current set；`CurrentStageClosureMapV1` 只解析 current set | TXT、manual-review JSON、alignment audit 和 completion projection |
| Stage plan | `runtime_job_spec_v1.json` 内持久化的 `stage_plan`，包含 requested/required stages、validation policy、current-set requirement 和 completion policy | job outcome 与 GUI 状态投影 |
| Repair | typed repair issue/action/patch、quarantine 派生输入、current service 重新验证及 receipt 闭包、带原子 current-set 切换的显式 versioned promotion transaction | 人类可读 repair 摘要 |

## 公开状态

`job_status` 为 `pending | running | completed | failed | cancelled`。

`job_disposition` 为 `clean | findings | needs_review | unvalidated`。

`claim_verdict` 为 `supported | partial_support | evidence_gap | unsupported |
contradicted | wrong_source | needs_review`。

缺少证据只能得到 `evidence_gap`，不会自动变成 `unsupported`。身份
`ambiguous/mismatch` 时可以完成诊断，但必须 quarantine，并保持
`canonical_ready=false`。

零 claim 的 Validation 永远是 `needs_review`；即使 review 声明
citation-free，也不能把未执行或空的验证变成 `clean`。成功 Validation
必须在规范 JSON 回读后确认 job ID、attempt ID、content hash 和精确
Registry dependency 闭包。
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

Stability policy 为 `off`、默认的 `smoke` 和 `full`。Smoke 执行一个额外的完整
reversed-summary decision chain 加 exact replay；full 执行完整 release/audit
矩阵。每个 node 都持久化 call/token/cost plan，并在 transport 前进行 preflight。
只有存在命名 pricing source 且五类 rate 齐全时才执行 monetary ceiling；否则
`cost_status=unknown`，仍执行 call/token ceiling，但不声称 monetary ceiling。
Provider 报告的 usage 只按本地 rate 计算，绝不冒充账单。Fresh executor 的
exact replay 必须记录 zero provider transport calls。

`reviewctl` 是唯一控制面。`status`、`next-action`、`validation-status`、
`inspect`、`attest` 是无 provider 的读取。`validate` 会真正执行当前
`ValidationExecutionService` 并持久化新的 validation attempt，不是只检查
已有 closure。`run`、`resume`、`retry-node`、`cancel`、
`repair-plan`、`repair-apply`、`adopt`、`export` 是显式的 Registry-backed 状态
迁移。Queue worker 以跨进程 lease generation 和 fence token claim job，必须
heartbeat，否则会失去 claim；过期 claim 可恢复，旧 worker 不能发布结果。Canonical
bytes 先在 lease 私有 staging 中生成，发布时先 queue store lock、后 Registry
transaction，并再次检查 fence；最终文件 immutable，且写入 publication manifest。
cancel
是 cooperative 的，被取消 job 不得发布为 completed。

Completion map 会聚合所有 required provider stage；validation 不是 analyze、
outline 或 review 的替代闭包。只要任一 stage-indexed closure 缺失，即使存在
历史 READY 产物或人类可读报告，也必须 fail-closed。

Validation closure 要求当前 review draft、citation manifest 与
`ValidationRunResultV1` 的输入 ID/hash 一致。若 validation 明确 optional 且禁用，
必须存在 `ValidationDispositionV1(status=not_requested, allow_unvalidated=true)`、
stage/spec/current-artifact hashes 和空 receipt closure；它只证明未请求，不能证明
验证通过。Repair 默认 `report_only`；显式
安全事务只创建 quarantine 的派生产物，并用当前 service 对精确文件重新验证
且闭合 receipt。只有 `repair-promote` 能创建新版本并推进 current pointer，
不会原地覆盖旧 canonical READY 文件。Adoption 不会静默提升中间 candidate。

Export bundle 包含已验证文件、provenance、checksum、completion evidence 和
validation-closure evidence。若 canonical 注册失败，导出状态为 `untrusted`，
ZIP path 和 artifact ID 都为空，并删除临时 bundle。`canonical_verified`、`manual_repaired`、
`untrusted` 是 attestation 标签，不是 job 成功别名；只有 DOCX 不能证明完成。
