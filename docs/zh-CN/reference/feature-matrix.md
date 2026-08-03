# 功能实现状态矩阵

状态对应当前 `codex/platform-hardening-outline-v3` 分支和最新离线验证。
`IMPLEMENTED` 表示组件存在，`INTEGRATED` 表示已接入生产调用方，
`E2E_VERIFIED` 表示当前生产形状链路实际跑过，`LIVE_VERIFIED` 只保留给真实
外部 Provider 运行，`NOT_VERIFIED` 表示所需 live 或 UI 证据尚未运行。

当前离线基线为 collected 678、passed 656、deselected 22。Live API、
Playwright 和 heavy OCR 本轮均为 `NOT_RUN`。

| 功能 | 状态 | 规范实现 |
|---|---|---|
| 来源 inventory 与 identity gate | E2E_VERIFIED | 内容 hash；DOI 或标题加真实作者/年份证据；match/ambiguous/mismatch；Stage 1 前 quarantine |
| Zotero 与 FileIndex | IMPLEMENTED | 带诊断解析、按 root 隔离、只读、多候选 |
| Artifact Registry v2 | E2E_VERIFIED | revision 锁事务、原子保存、READY 本地/跨 job 依赖即时 fail-closed 校验 |
| Job outcome 与 attempts | E2E_VERIFIED | job outcome、append-only attempts、pointer 所有权 |
| Stage 1 与证据 | E2E_VERIFIED | 不可变 content-addressed summaries、paper artifact、evidence manifest、edge checkpoint；summary -> source_bundle -> 来源 PDF 依赖链 |
| ReviewBatch 派生 | INTEGRATED | 固定父 hash、child Stage 1 调用为零、derivation/coordinator lease、单调 generation、immutable max-head projection receipt |
| Outline Intelligence v3 | E2E_VERIFIED | 已注册 evidence views、确定性 node DAG、精确 execution/replay 闭包、typed quality gate、stability variants、health 和显式 versioned adoption |
| Review/Citation/DOCX | E2E_VERIFIED | 当前 review draft v3、完整 section binding、manifest v3、token spans、实际引用 bibliography、DOCX |
| Validation 真相源 | E2E_VERIFIED | 显式 `ValidationExecutionService` constructor 和 current runner boundary；transport 前 request binding；response/normalized/artifact/node receipt 闭包；持久化回读并绑定 job/attempt/hash 的 `ValidationRunResultV1`；review/citation/evidence `depends_on` 精确闭包；其他报告仅为投影 |
| Outline quality 与 stability gates | E2E_VERIFIED | typed `OutlineQualityGate`、effective-section/duplicate/placeholder/empty-stream audit、真实 provider stability variants、full-decision comparison 和 gate-hash invalidation |
| Repair promotion boundary | INTEGRATED | typed issue/action/auto-safe patch、semantic revalidation、quarantined 派生版本和不覆盖 canonical 的 versioned draft/manifest/DOCX/audit/lineage promotion |
| AgentRuntimeRunner | E2E_VERIFIED | 在 `AgentRuntimeBridge` 上提供 run/resume/status/reconcile；持久化 `BaseException` 终态并从规范 artifact 恢复 Validation disposition |
| Queue 状态映射 | INTEGRATED | Queue 读取 `job_status`；旧 success 仅为 readiness 投影；worker lease 有独立 heartbeat 和 lease-loss fence |
| MinerU/Docling/OCR 稳定性 | IMPLEMENTED | preflight、共享认证熔断、受控子进程 timeout |
| Windows 输出 | IMPLEMENTED | UTF-8 控制台与 ASCII-safe JSON progress |
| GUI workflow 与 queue | INTEGRATED | 本地 workflow UI、跨进程原子 queue snapshot、CAS worker lease、heartbeat、expiry/crash recovery 和 serial persistent queue |
| 过期 workspace | IMPLEMENTED | 缺少当前 identity/readiness 字段时拒绝进入运行，保持非 ready |
| 真实 provider smoke | NOT_VERIFIED | 必须具备 marker、显式开关和凭据；本轮未调用 live provider |

未来工作必须写成带责任边界和自动化验收的具体 limitation；不能把 deterministic
offline 证据描述为 live verification。
