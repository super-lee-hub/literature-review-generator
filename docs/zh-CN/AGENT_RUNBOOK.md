# Agent 操作手册

`reviewctl` 是现有任务工作区的控制面。读取命令无 Provider；`validate` 是
明确执行当前 `ValidationExecutionService` 并持久化新的 validation attempt 的
命令。控制面读取 Registry、job outcome、stage terminal、API receipt 以及
Outline v3 DAG/replay。

完成与导出先通过 `current-artifact-set:pointer` 解析原子
`CurrentArtifactSetV1`，再构建 `CurrentStageClosureMapV1`；历史 READY 产物
不能替代 current set。

持久化 `StagePlan` 控制 completion。validation 启用时，`run_all` 请求
analyze、outline、review、validate；明确设为 optional 且禁用时只请求
analyze、outline、review，但仍要求 current set。派生和 outline-only 操作
没有 current set 时不能 canonical-ready；中间 Outline v3 candidate 不会被
静默 adoption。

## 安全顺序

```text
python -m reviewctl doctor --config <config.ini>
python -m reviewctl status --job <job_id>
python -m reviewctl next-action --job <job_id>
python -m reviewctl validate --job <job_id>
python -m reviewctl validation-status --job <job_id>
python -m reviewctl repair-plan --job <job_id>
python -m reviewctl export --job <job_id>
python -m reviewctl attest --job <job_id>
```

找不到任务 ID 时使用 `--workspace <workspace_path>`。所有命令只输出一个机器可读 JSON 对象。

`run` 开始新的 runtime attempt，`resume` 只复用已由 Registry 验证的持久化阶段；`retry-node` 只重试持久化的失败 Outline v3 节点；`reconcile --dry-run` 只读；`cancel` 写入协作式取消请求，不杀进程。Worker 在安全检查点观察请求，之后不得发布 `completed`。

Queue worker 使用原子 snapshot、input/config fingerprint、lease generation 和
fence token。过期或被 fence 的 worker 不得发布结果；retry、cancel、恢复都必须
以持久化 queue 状态为准。

Canonical bytes 先写入 lease generation 私有 staging。发布时先取得 queue store
lock，复核 lease/worker/generation/fence，再进入 Registry transaction；该
queue -> Registry 锁顺序是契约的一部分。最终文件 immutable，并记录 staged/final
hash 的 publication manifest。包括 Windows `spawn` 子进程在内，claim 已过期的旧
worker 即使持有过期本地 queue snapshot，也不能发布 canonical artifact。

`validate` 会对当前 v3 review draft、v3 citation manifest 和 evidence 输入真正执行
当前 Validation，并持久化 `ValidationRunResultV1`、receipt 与 Registry 依赖。若
validation 明确 optional 且禁用，runner 改为持久化 typed
`ValidationDispositionV1(status=not_requested, allow_unvalidated=true)` 与空 closure；
这只证明未请求，不代表 Validation 通过。
`validation-status` 才是只读的 closure 检查，包含 Registry 身份和哈希相等性。
`repair-plan` 默认 report-first，只能写入哈希绑定的计划和事务记录；明确标记为
`auto_apply_safe` 的 `repair-apply` 也只生成 `quarantined` 派生版本，不替换
canonical READY 文件。`repair-promote --transaction <id> --actor <actor> --reason
<reason>` 必须先通过当前 service 重新验证和完整 closure，之后才能创建新版本并推进
current pointer。

`adopt --artifact <final_outline_id> --actor <actor>` 是显式采用操作，要求 final outline、coverage audit、stage health、哈希和 blocking critique 门禁全部通过；Outline v3 candidate plan 不会被静默提升。`export` 和 `attest` 会生成 provenance、checksum、completion、validation closure 与依赖图证据；canonical 注册失败会返回 `untrusted`、空 path/id，并删除临时 ZIP。

Outline stability 有 `off`、`smoke`、`full` 三种模式：smoke 增加一个完整
reversed-summary decision chain 和 exact replay，full 执行完整 perturbation matrix。
每个 node 的 call/token/cost plan 都会持久化；只有命名 pricing source 和完整 rate
存在时才执行 monetary ceiling，未知价格记录为 `cost_status=unknown`，本地计算
usage 不等于 Provider billing。

信任状态只有 `canonical_verified`、`manual_repaired` 和 `untrusted`。不能因为磁盘上有 DOCX、报告文本或手工编辑过的 Registry/Stage Health 就宣称完成。
