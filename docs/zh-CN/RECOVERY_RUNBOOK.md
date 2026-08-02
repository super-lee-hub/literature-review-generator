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
- validation closure 缺失：运行 `validate`，修复输入链或重新验证；旧文本报告不是事实源。
- pending/running 队列任务：使用 `cancel`；新一轮 resume/retry 才能清除取消标记。
- adoption 失败：检查 coverage audit、stage health、final-outline hash 和 blocking critique，不得绕过门禁。

```text
python -m reviewctl resume --workspace <workspace>
python -m reviewctl validate --workspace <workspace>
python -m reviewctl export --workspace <workspace>
```

`untrusted` 导出包只能作为取证材料，不能当作完成的 review 发布。不得手工改写 `artifact_registry.json`、Stage Health 或删除工作区。
