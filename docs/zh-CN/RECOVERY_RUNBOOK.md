# 恢复手册

恢复必须先保留工作区并查看证据，不能先覆盖或删除文件。

```text
python -m reviewctl status --workspace <workspace>
python -m reviewctl inspect --workspace <workspace>
python -m reviewctl next-action --workspace <workspace>
python -m reviewctl attest --workspace <workspace>
```

这些输出会显示任务状态、失败节点、Provider error kind、Registry 完整性、依赖图以及已保留的完成节点。

- quota、可重试 HTTP、临时网络或 invalid response：先看 receipt；只有 `safe_to_retry=true` 才重试失败节点。
- artifact 过期或篡改：不要从它 resume；先运行 `reconcile --dry-run`，保留证据，再生成 report-only repair plan。
- validation closure 缺失：运行 `validate` 执行当前 Validation service，再用
  `validation-status` 查看持久化 closure；修复输入链或重新验证，旧文本报告不是事实源。
  若 validation 明确 optional 且禁用，必须检查 typed
  `ValidationDispositionV1/v1(status=not_requested, allow_unvalidated=true)`、
  `validation_required=false`、`validation_enabled=false`、当前
  `CurrentArtifactSet` 绑定和 zero-call closure；它不是“验证通过”的证据。
- pending/running 队列任务：使用 `cancel`；新一轮 resume/retry 才能清除取消标记。
  丢失 lease heartbeat 的 worker 会被 fence，不得完成或释放旧 claim。Queue 发布
  先把 bytes 放入 lease 私有 staging，再按 queue store -> Registry 锁顺序发布；
  target 与 `lease_publication_manifest` 必须原子提交。Registry 失败留下的 immutable
  orphan 应保留为证据，不得恢复 fixed target；既有相同 hash 文件不得因 alias 失败
  被删除，字节不同的碰撞须在 Registry mutation 前阻断。
- current set target 类型或版本错误：停止恢复并保留证据。`switch_current_artifact_set`
  与 `resolve_current_artifact_set` 都必须验证每个 target 的类型/版本以及 promotion
  validation evidence；任意 READY JSON 不能替代规范 target。
- adoption 失败：检查 coverage audit、stage health、final-outline hash 和 blocking critique，不得绕过门禁。
- `run_all` 如果在 validation 前停止，不能直接当作完成；先检查持久化
  `StagePlan` 和 current-set requirement。若 validation 是 optional 且禁用，
  analyze/outline/review 仍须有完整 stage-indexed closure 和 current set；若
  validation 是 required，则必须补跑缺失的 `validate` 阶段。
- zero-call 阶段只有在 expected-call graph 及依赖有效、terminal model call 和
  observed receipt 都为零、且 typed source evidence 存在时才算完成；不能伪造空
  receipt ledger。all-reuse Stage 1 还要检查 SourceBundle identity 唯一覆盖和真实
  Registry source-artifact 绑定。

```text
python -m reviewctl resume --workspace <workspace>
python -m reviewctl validate --workspace <workspace>
python -m reviewctl validation-status --workspace <workspace>
python -m reviewctl export --workspace <workspace>
```

`canonical_unvalidated` 只有在完整 typed not-requested policy 和 stage closure 下才可
发布，provenance 必须明确 semantic validation 未执行。`untrusted` 导出包只能作为
取证材料，不能当作完成的 review 发布。不得手工改写 `artifact_registry.json`、Stage
Health 或删除工作区。
