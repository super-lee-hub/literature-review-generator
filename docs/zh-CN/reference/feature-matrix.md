# 功能实现状态矩阵

| 功能 | 状态 | 规范实现 |
|---|---|---|
| 来源 inventory 与 identity gate | 已实现 | 内容 hash；DOI 或标题加真实作者/年份证据；match/ambiguous/mismatch；Stage 1 前 quarantine |
| Zotero 与 FileIndex | 已实现 | 带诊断解析、按 root 隔离、只读、多候选 |
| Artifact Registry v2 | 已实现 | revision 锁事务、原子保存、READY 本地/跨 job 依赖即时 fail-closed 校验 |
| Job outcome 与 attempts | 已实现 | job outcome、append-only attempts、pointer 所有权 |
| Stage 1 与证据 | 已实现 | 规范 summaries、paper artifact、evidence manifest、edge checkpoint；summary -> source_bundle -> 来源 PDF 依赖链 |
| ReviewBatch 派生 | 已实现 | 固定父 hash、child Stage 1 调用为零、derivation/coordinator lease、单调 generation、immutable max-head projection receipt |
| Outline Intelligence v3 | 已实现 | 已注册 evidence views、确定性 node DAG、replay receipts、health 和显式 adoption |
| Review/Citation/DOCX | 已实现 | 当前 review draft v3、manifest v3、实际引用 bibliography、DOCX |
| Validation 真相源 | 已实现 | `ValidationRunResultV1` 持久化回读并绑定 job/attempt/hash；review/citation/evidence `depends_on` 精确闭包；输入为 64 位小写 SHA-256；零 claim 必须显式 citation-free；evidence 失效后 terminal/resume 不得复用；其他报告仅为投影 |
| AgentRuntimeRunner | 已实现 | 在 `AgentRuntimeBridge` 上提供 run/resume/status/reconcile；持久化 `BaseException` 终态并从规范 artifact 恢复 Validation disposition |
| Queue 状态映射 | 已实现 | Queue 读取 `job_status`，旧 success 仅为 readiness 投影 |
| MinerU/Docling/OCR 稳定性 | 已实现 | preflight、共享认证熔断、受控子进程 timeout |
| Windows 输出 | 已实现 | UTF-8 控制台与 ASCII-safe JSON progress |
| 过期 workspace | fail-closed | 缺少当前 identity/readiness 字段时拒绝进入运行，保持非 ready |
| 真实 provider smoke | 可选 | 必须同时具备 marker、显式开关和凭据 |

已标记“已实现”的功能不再重复列入 roadmap。未来工作必须写成带责任边界和自动化验收的具体 limitation。
