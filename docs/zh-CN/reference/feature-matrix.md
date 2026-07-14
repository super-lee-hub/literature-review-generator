# 功能实现状态矩阵

| 功能 | 状态 | 规范实现 |
|---|---|---|
| 来源 inventory 与 identity gate | 已实现 | 内容 hash、match/ambiguous/mismatch、Stage 1 前 quarantine |
| Zotero 与 FileIndex | 已实现 | 带诊断解析、按 root 隔离、只读、多候选 |
| Artifact Registry v2 | 已实现 | revision 锁事务、原子保存、本地/跨 job V2 依赖 |
| Job outcome 与 attempts | 已实现 | job outcome、append-only attempts、pointer 所有权 |
| Stage 1 与证据 | 已实现 | 规范 summaries、paper artifact、evidence manifest、edge checkpoint |
| ReviewBatch 派生 | 已实现 | 固定父 hash，child Stage 1 调用为零 |
| Outline Intelligence v2 | 已实现 | 完整 artifact 链、预算、health sidecar、显式采纳 |
| Review/Citation/DOCX | 已实现 | review draft v2、manifest v3、实际引用 bibliography、DOCX |
| Validation 真相源 | 已实现 | `ValidationRunResultV1`；其他报告仅为投影 |
| AgentRuntimeRunner | 已实现 | 在 `AgentRuntimeBridge` 上提供 run/resume/status/reconcile |
| Queue 状态映射 | 已实现 | Queue 读取 `job_status`，旧 success 仅为 readiness 投影 |
| MinerU/Docling/OCR 稳定性 | 已实现 | preflight、共享认证熔断、受控子进程 timeout |
| Windows 输出 | 已实现 | UTF-8 控制台与 ASCII-safe JSON progress |
| 旧 workspace | 兼容读取 | 新字段缺失时标记 `legacy_unverified`，不默认 ready |
| 真实 provider smoke | 可选 | 必须同时具备 marker、显式开关和凭据 |

已标记“已实现”的功能不再重复列入 roadmap。未来工作必须写成带责任边界和自动化验收的具体 limitation。
