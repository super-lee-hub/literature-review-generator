# 功能实现状态矩阵

状态对应当前 `codex/platform-hardening-outline-v3` 分支和最新离线验证。
`IMPLEMENTED` 表示组件存在，`INTEGRATED` 表示已接入生产调用方，
`CONTROLLER_VERIFIED` 只表示 controller/label 边界已验证，不宣称浏览器自动化，
`E2E_VERIFIED` 表示当前生产形状链路实际跑过，`LIVE_VERIFIED` 只保留给真实
外部 Provider 运行，`NOT_VERIFIED` 表示所需 live 或 UI 证据尚未运行。

当前离线 collection 为 784；严格 marker 选择为 selected 762、deselected 22，
严格 aggregate 已通过（`762 passed, 22 deselected`，约 16:50）。Live API、
Playwright 和 heavy OCR 本轮仍为 `NOT_RUN`。

| 功能 | 状态 | 规范实现 |
|---|---|---|
| 来源 inventory 与 identity gate | E2E_VERIFIED | 内容 hash；DOI 或标题加真实作者/年份证据；match/ambiguous/mismatch；Stage 1 前 quarantine |
| Zotero 与 FileIndex | IMPLEMENTED | 带诊断解析、按 root 隔离、只读、多候选 |
| Artifact Registry v2 | E2E_VERIFIED | revision 锁事务、typed multi-record 原子保存、READY 本地/跨 job 依赖即时 fail-closed 校验，以及对既有相同 hash immutable publication target 的保护 |
| Job outcome 与 attempts | E2E_VERIFIED | job outcome、append-only attempts、pointer 所有权 |
| Stage 1 与证据 | E2E_VERIFIED | 不可变 content-addressed summaries、paper artifact、evidence manifest、typed `stage1_reusable_summary_manifest/v1` source manifest、edge checkpoint；zero-call closure、all-reuse/mixed-reuse provenance、真实 Registry source-artifact binding，且不伪造 receipt ledger；summary -> source_bundle -> 来源 PDF 依赖链 |
| ReviewBatch 派生 | INTEGRATED | 固定父 hash、child Stage 1 调用为零、derivation/coordinator lease、单调 generation、immutable max-head projection receipt |
| Outline Intelligence v3 | E2E_VERIFIED | 已注册 artifact validation surface、确定性 node DAG、精确 execution/replay 闭包、typed quality gate、`off`/`smoke`/`full` stability、调用/成本 preflight、checkpointed subruns、health、critic retry scope 和显式 versioned adoption |
| Review/Citation/DOCX | E2E_VERIFIED | 当前 review draft v3、完整 section binding、manifest v3、token spans、实际引用 bibliography、DOCX |
| Validation 真相源 | E2E_VERIFIED | 显式 `ValidationExecutionService` constructor 和 current runner boundary；transport 前 request binding；response/normalized/artifact/node receipt 闭包；持久化回读并绑定 job/attempt/hash 的 `ValidationRunResultV1`；optional disabled 时使用 typed `ValidationDispositionV1(status=not_requested, allow_unvalidated=true)` 和空 closure；review/citation/evidence `depends_on` 精确闭包；其他报告仅为投影 |
| Stage plan 与 `run_all` policy | E2E_VERIFIED | validation 启用时持久化 plan 固定为 analyze/outline/review/validate；禁用时只省略 optional validation 并发布 typed not-requested disposition 与 zero-call validation closure，但仍要求 `CurrentArtifactSet`；派生或 outline-only 不能在没有 current set 时 canonical-ready |
| Outline quality 与 stability gates | E2E_VERIFIED | typed `OutlineQualityGate`、effective-section/duplicate/placeholder/empty-stream audit、一个额外的完整 reversed-summary smoke chain、full-decision stability variants、逐节点 call/token/cost plan、provider/model 绑定 pricing、硬 call/context/prompt/total-token admission、zero-transport exact replay 和 gate-hash invalidation |
| Repair promotion boundary | E2E_VERIFIED | typed issue/action/auto-safe patch、current service revalidation、quarantined 派生版本、原子 `CurrentArtifactSet` 切换以及不覆盖 canonical 的 versioned draft/manifest/DOCX/audit/lineage promotion |
| AgentRuntimeRunner | E2E_VERIFIED | 在 `AgentRuntimeBridge` 上提供 run/resume/status/reconcile；持久化 `BaseException` 终态并从规范 artifact 恢复 Validation disposition |
| Queue 状态映射 | E2E_VERIFIED | Queue 读取 `job_status`；旧 success 仅为 readiness 投影；lease-generation staging 按 queue-lock -> Registry 顺序发布 immutable bytes；target 与 lease publication manifest 原子提交；失败只留下未引用 immutable bytes；重复/direct alias 保护既有相同文件，并覆盖 Windows `spawn` stale-worker current-set race |
| Trust-bound canonical export | E2E_VERIFIED | `canonical_verified` 与真实 `canonical_unvalidated` 都通过 completion、typed disposition、CurrentArtifactSet、stage closure、Registry dependency、ZIP provenance 和 forensic read-back；unvalidated 明确警告且不声称 clean validation |
| Publication architecture gate | E2E_VERIFIED | 当前 writer 使用 publication boundary；architecture scan 拒绝 canonical path 写/替换后再单独 Registry 注册，仅允许狭窄的 private staging/cache/rendering/read-only legacy 例外 |
| MinerU/Docling/OCR 稳定性 | IMPLEMENTED | preflight、共享认证熔断、受控子进程 timeout |
| Windows 输出 | IMPLEMENTED | UTF-8 控制台与 ASCII-safe JSON progress |
| GUI workflow 与 queue | CONTROLLER_VERIFIED | 本地 workflow controller/status label、跨进程原子 queue snapshot、CAS worker lease、heartbeat、expiry/crash recovery 和 serial persistent queue；Playwright 尚未运行 |
| 过期 workspace | IMPLEMENTED | 缺少当前 identity/readiness 字段时拒绝进入运行，保持非 ready |
| 真实 provider smoke | NOT_VERIFIED | 必须具备 marker、显式开关和凭据；本轮未调用 live provider |

未来工作必须写成带责任边界和自动化验收的具体 limitation；不能把 deterministic
offline 证据描述为 live verification。
