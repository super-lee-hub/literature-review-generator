# 兼容性契约

兼容读取必须是 additive 且 fail-closed：旧 artifact 可以读取，但不会因此自动获得新的身份、Validation 或 readiness 保证。

## 旧 workspace

- 缺少 `SourceInventoryV1`、readiness policy、attempt history 或 V2 dependency identity 时，投影为 `legacy_unverified`。
- 旧 `success` 只兼容投影 `canonical_ready`；Queue 状态不再从它推导。
- 旧 Validation report 可经 adapter 读取，但不满足 `ValidationRunResultV1`。
- 旧 Registry dependency 尽可能归一化为 V2；缺 artifact identity/hash 时不得推断为 ready。
- 仅在显式关闭 Outline v2 时允许旧 Markdown outline；v2 开启后，缺少当前已注册 adopted outline 或 health sidecar 必须 fail-closed。

`status` 和 `reconcile` 对仅含 summary 的旧 workspace 都是只读操作：它们只报告 `legacy_unverified` 以及“需要显式迁移或重新运行”，不会创建 Registry、job outcome 或 audit record。唯一公开的兼容迁移入口是：

```powershell
python -m runtime.cli migrate-legacy <workspace> --actor <operator> --reason <reason>
```

`--actor` 与 `--reason` 均为必填。该命令不会调用 provider，只会物化 fail-closed 兼容头：`compatibility_status=legacy_unverified`、`canonical_ready=false`、`requires_attention=true`，并生成不可变 `AuditRecordV1`。使用相同参数重复迁移必须保持字节级幂等；native 或非 summary-only workspace 会被拒绝。迁移绝不会把旧证据升级为 canonical readiness。

## 必须审计的兼容动作

显式复用旧 summary、ambiguous identity 人工选择、Outline 人工采纳、force delete 和 quarantine release 都必须生成不可变 `AuditRecordV1`，记录 actor、reason、scope、input hashes、policy snapshot 以及 artifact ID/hash。项目不支持长期布尔绕过开关。

## 路径与跨 job 依赖

- spec/config/summary 内的相对路径分别按其所属文件目录解析。
- 跨 job 依赖使用 `external_job`；`job_id + artifact_id + content_hash` 是身份，path 只是定位投影。
- 父 artifact 有未失效 child dependency 时默认禁止删除；force delete 必须写审计并使相关 child 失效。

## 可选集成边界

live API、Playwright 与 heavy OCR 测试属于 optional marker，必须显式启用并满足前置条件。strict-offline 测试禁止外部网络、允许 loopback，并把离线边界传播到 Python 子进程。
