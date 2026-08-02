# Agent 操作手册

`reviewctl` 是现有任务工作区的无 Provider 控制面。它读取 Registry、job outcome、stage terminal、API receipt 以及 Outline v3 DAG/replay，不直接编辑这些事实源。

## 安全顺序

```text
python -m reviewctl doctor --config <config.ini>
python -m reviewctl status --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl repair-plan --job <job_id>
python -m reviewctl export --job <job_id>
python -m reviewctl attest --job <job_id>
```

找不到任务 ID 时使用 `--workspace <workspace_path>`。所有命令只输出一个机器可读 JSON 对象。

`run` 开始新的 runtime attempt，`resume` 只复用已由 Registry 验证的持久化阶段；`retry-node` 只重试持久化的失败 Outline v3 节点；`reconcile --dry-run` 只读；`cancel` 写入协作式取消请求，不杀进程。Worker 在安全检查点观察请求，之后不得发布 `completed`。

`validate` 检查 v2 review draft、v3 citation manifest 与 `ValidationRunResultV1` 的 Registry 身份和哈希闭环。`repair-plan` 默认 report-first，只能写入哈希绑定的计划和事务记录；明确标记为 `auto_apply_safe` 的 `repair-apply` 也只生成 `quarantined` 派生版本，不替换 canonical READY 文件。

`adopt --artifact <final_outline_id> --actor <actor>` 是显式采用操作，要求 final outline、coverage audit、stage health、哈希和 blocking critique 门禁全部通过；Outline v3 candidate plan 不会被静默提升。`export` 和 `attest` 会生成 provenance、checksum、completion、validation closure 与依赖图证据。

信任状态只有 `canonical_verified`、`manual_repaired` 和 `untrusted`。不能因为磁盘上有 DOCX、报告文本或手工编辑过的 Registry/Stage Health 就宣称完成。
