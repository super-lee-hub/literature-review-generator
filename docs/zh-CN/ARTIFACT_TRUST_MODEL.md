# Artifact 信任模型

信任来自可持久化、哈希绑定的事实，而不是文件名或进程退出码。

| 层 | canonical 证据 | 规则 |
|---|---|---|
| Source identity | `source_inventory_v1.json`、source bundle、注册的 paper artifact | identity 不确定时保持 quarantine |
| Artifact graph | `artifact_registry.json` v2 | READY 的路径、哈希、schema 和依赖必须验证 |
| Runtime | `job_outcome_v1.json`、append-only attempt、stage terminal | 由 `CanonicalCompletionEvaluator` 判定完成 |
| Outline | 注册的 v2 链和 Outline v3 evidence/ledger/matrix/relation/DAG | candidate/replay 不是采用后的事实 |
| Review | review draft v2 与 citation manifest v3 | citation 身份和渲染策略由 manifest 驱动 |
| Validation | `ValidationRunResultV1` 及精确 Registry 输入闭环 | 人类可读报告只是投影 |
| Repair | 哈希绑定的 report-first plan 和 transaction | 应用结果是新的 quarantined artifact |
| Export | 声明式 bundle 与 forensic attestation | 信任标签必须明确、可复现 |

`ready` 表示 Registry 能验证文件和所有必需依赖；`quarantined` 表示保留供审查但不能作为 canonical 输入；`invalid` 不得用于 resume 或发布。

v3、validation、repair、export、adoption 都不会覆盖 canonical artifact。显式 adoption 只有在 final-outline、coverage-audit、stage-health 和 completion 门禁全部通过后才创建 `adopted_final_outline`。取消是协作式状态转换，并记录为派生请求 artifact。
