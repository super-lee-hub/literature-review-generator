# Windows 操作手册入口

通用的 PDF 到 DOCX 与 GUI 操作说明见[Windows 操作手册](../../zh-CN/AGENT_RUNBOOK.md)。本文是 PR25 当前运行面、SourceRevision 前提及从 Outline 采用到可信导出的补充操作入口；它不继承历史文档中的旧入口状态作为当前验收。来源、阶段、回执、预算和恢复都通过同一个 Registry-backed runtime 管理。

此恢复包不包含私人全文、密钥、运行账本、已付款缓存或项目工作区，也不授予新的外部调用额度。恢复包所列 Git SHA 只标识包内源码快照，不代表已推送 PR、当前工作区或完整运行已验收。

## 1. 入口、源码和 Python

生产运行前必须使用维护者确认的最终干净 SHA、匹配的 Python 3.11 环境和该源码版本的生产依赖锁。当前 dirty checkout、旧 `.bat` 路由或开发入口本身都不是发布验收证据。不要切换到已删除的 `main.py` 直接参数流程。

`reviewctl` 是公共 CLI，执行输入是 `RuntimeJobSpec`：

```text
RuntimeJobSpec -> ReviewControlPlane -> AgentRuntimeRunner
               -> AgentRuntimeBridge -> Registry-backed stages
```

GUI 从 `launch_gui.py` / `gui/app.py` 进入相同的 `RuntimeJobSpec`、Runner、Bridge 和 Registry 契约。GUI 与 CLI 不是两套任务状态。只有在 GUI 包装入口也固定到最终 clean SHA 和匹配解释器后，才把它用作正常入口。

在 PowerShell 中，将路径替换为实际 checkout、最终发布 SHA 和工作区路径：

```powershell
$RepoRoot = (Resolve-Path -LiteralPath (Read-Host '最终干净 checkout 的绝对路径')).Path
$ExpectedSha = Read-Host '发布记录中的最终源码 SHA'
$ActualSha = (& git -C $RepoRoot rev-parse HEAD).Trim()
$GitExit = $LASTEXITCODE
$Dirty = @(& git -C $RepoRoot status --porcelain)
$StatusExit = $LASTEXITCODE
if ($GitExit -ne 0 -or $StatusExit -ne 0 -or $ActualSha -cne $ExpectedSha -or $Dirty.Count -ne 0) {
    throw '源码 SHA 不匹配或 checkout 不干净；不要开始真实运行。'
}

$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    py -3.11 -m venv (Join-Path $RepoRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 venv creation failed.' }
}
$PythonVersion = (& $Python --version 2>&1 | Out-String).Trim()
if ($PythonVersion -notmatch '^Python 3\.11\.') { throw "需要 Python 3.11；当前为 $PythonVersion" }
& $Python -m pip install -r (Join-Path $RepoRoot 'requirements-py311-windows-prod.lock')
if ($LASTEXITCODE -ne 0) { throw '生产依赖锁安装失败。' }
& $Python -m reviewctl --help
if ($LASTEXITCODE -ne 0) { throw '固定环境中的 reviewctl CLI 不可用。' }
```

`.venv` 已存在时，先核对它由选定的 Python 3.11 创建，再安装对应锁文件。不要以当前旧入口、dirty 工作树、fixture 输出或本地进程启动代替最终源码与解释器固定。

## 2. 配置与任务 spec

从同一源码版本的 `config.ini.example` 建立私人 `config.ini`；保留已有自定义设置，不覆盖现存配置。实际密钥放在私人配置或其规定的 `.env` 中，不提交。设置真实 `Paths.output_path`、Reader、Outline、Writer、Validator 路由，以及 `[OutlineModels]` 各角色映射。`api_base` 只表示请求目标；第三方 gateway 不证明其上游物理模型身份。预处理远程解析器的凭据和允许上传的主机也需要单独审查。

`RuntimeJobSpec` 顶层没有 `schema_version`。它至少绑定项目、来源、配置、动作、唯一 job ID 和工作区；运行控制 metadata 放在 `metadata` 中。使用 `doctor` 和 `preflight` 做配置/路由检查：

```powershell
$Config = (Resolve-Path -LiteralPath (Read-Host '私人 config.ini 的绝对路径')).Path
& $Python -m reviewctl doctor --config $Config
& $Python -m reviewctl preflight --config $Config --action run_all
```

这些命令不授权 Provider 请求，也不证明账户额度、真实网络可用或论文结论正确。`plan --spec` 用于审阅来源、阶段和已知/未知请求暴露；不能把默认预算、规划下界或 `planned` 状态当作真实运行批准。生产 spec 应包含经任务所有者审定的 `metadata.provider_budget` 严格 V2 对象，字段固定为：

- `schema_version`: `provider-aggregate-budget-contract-v2`
- `max_provider_calls_total`
- `max_output_tokens_total`
- `max_retry_attempts_total`
- `max_wall_seconds`

上限必须有限并按实际请求计划批准；零代表零额度，不代表无限。运行时按真实节点、输出 reserve 和尝试数进行 JIT reservation，累计消耗与未结 reservation 受同一预算约束。计划估算不是完整账单或实际请求数。

```powershell
$Spec = (Resolve-Path -LiteralPath (Read-Host '已审阅 RuntimeJobSpec JSON 的绝对路径')).Path
$Plan = & $Python -m reviewctl plan --spec $Spec | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $Plan.status -ne 'planned') { throw 'Spec planning did not complete; do not run.' }
$Plan | ConvertTo-Json -Depth 64
```

`run --spec` 才会创建或执行真实任务，可能产生 Provider 请求：

```powershell
& $Python -m reviewctl run --spec $Spec
```

运行后的 `status`、`inspect`、`next-action` 和 `validation-status` 均通过 workspace/Registry 读回状态；恢复现有任务使用 `resume --workspace`，不要用新 job ID 重放旧请求。

## 3. SourceRevision：只允许严格绑定的派生运行

SourceRevision 不是跨任务通用缓存开关。生产计划要求 parent 和 child 两份新建时的 RuntimeJobSpec 都显式设置：

```json
"provider_semantic_contract_version": "source-closed-provider/v2"
```

新建 parent spec 必须在首次 `plan`/`run` 前，于 `metadata` 中显式设置该字段；child spec 也要显式设置：

```powershell
$ParentSpec.metadata.provider_semantic_contract_version = 'source-closed-provider/v2'
$ChildSpec.metadata.provider_semantic_contract_version = 'source-closed-provider/v2'
```

缺省版本按 legacy/v1 处理。旧 v1 parent 不能授予 successor reuse；不能回写其 spec、迁移或改写旧的已付款 ledger 来补出 v2。只有另行授权并从一开始声明 v2 的新 parent 才能为未来的 SourceRevision 建立资格。

当前 `replace-existing-pdf/v1` 仅支持：parent 是完整、成功且 clean-validated 的 canonical 全流程；parent/child 使用相同配置字节和相同 ReviewIntent；PDF 身份键集合及顺序相同且唯一，恰有一个 PDF 的字节 SHA 变化；child 使用新的 job ID 和独立 workspace，并有自己的有限预算。已验证 PDF locator/fingerprint 的 v2 规范化不会删掉 Stage 1 正文或 source audit。逐次 response lookup 中，request、prompt、route/config、schema 或 source scope 不完全匹配时一律禁止复用 parent response；只有计划仍满足 source/config authority 的子任务才能在自己的原预算内重新生成。会破坏 source revision admission 的 config/source closure 变化应被拒绝并重新审阅，不得当作普通 cache miss 绕过。计划生成成功也不保证一定能复用任何 response。

在 child spec 中先显式填写 v2、与 parent 完全相同的 `metadata.review_intent`、child 的有限 `metadata.provider_budget`、新 `job_id`、`action=run_all` 和新的 `workspace_path`。`source-revision-plan` 会重新核验 parent Registry/闭包和 PDF 字节，只写计划文件，不创建 child workspace、不发 Provider POST、不改变 parent；输出文件必须是新路径并位于 parent/child workspace 之外。命令同时返回绑定了 `metadata.source_revision_plan` 的 `normalized_child_spec`，应保存该完整 JSON 并用它运行：

```powershell
function Write-Utf8Json($Path, $Value) {
    $Json = $Value | ConvertTo-Json -Depth 64
    [IO.File]::WriteAllText($Path, $Json, [Text.UTF8Encoding]::new($false))
}

$ParentWorkspace = (Resolve-Path -LiteralPath (Read-Host '已完成 parent 的 workspace 绝对路径')).Path
$ChildSpec = (Resolve-Path -LiteralPath (Read-Host '已审阅 child spec 的绝对路径')).Path
$Work = Read-Host 'parent 和 child workspace 之外的新 SourceRevision 工作目录'
if (Test-Path -LiteralPath $Work) { throw '工作目录已存在；选择一个新的独立目录。' }
[void](New-Item -ItemType Directory -Path $Work)
$Work = (Resolve-Path -LiteralPath $Work).Path
$SourcePlanPath = Join-Path $Work 'source-revision-plan.json'
$BoundChildSpec = Join-Path $Work 'child-bound.json'
if (Test-Path -LiteralPath $SourcePlanPath) { throw '计划输出已存在；选择新的空工作目录。' }

$SourcePlan = & $Python -m reviewctl source-revision-plan `
    --spec $ChildSpec --parent-workspace $ParentWorkspace --output $SourcePlanPath | ConvertFrom-Json
$PlanExitCode = $LASTEXITCODE
if ($PlanExitCode -ne 0 -or $SourcePlan.status -ne 'source_revision_planned') {
    throw 'SourceRevision admission 未通过；保留证据，不要运行 child。'
}
Write-Utf8Json $BoundChildSpec $SourcePlan.normalized_child_spec
& $Python -m reviewctl plan --spec $BoundChildSpec
& $Python -m reviewctl run --spec $BoundChildSpec
```

保存并保留 parent workspace、child spec、计划文件、Registry、回执、预算账本、输入 PDF 和最终导出。若 admission 拒绝 v1 parent 或任一身份绑定，不要修改历史记录来绕过。

## 4. Outline 采用、Writer、Validator 与修复

Outline 候选不会自动变成下游 authority。先用 `inspect` 查看当前 Registry artifacts，人工阅读拟采用的最终 Outline；再从同一返回记录复制准确的 `artifact_id` 和 `content_hash`。采用命令重新执行 gate，不按文件名猜 ID：

```powershell
$Workspace = (Resolve-Path -LiteralPath (Read-Host '当前任务 workspace 的绝对路径')).Path
& $Python -m reviewctl inspect --workspace $Workspace
$ArtifactId = Read-Host '已审阅的最终 Outline artifact_id'
$ExpectedHash = Read-Host '同一 READY artifact 的精确 content_hash'
$Actor = Read-Host '实际采用人'
$Reason = Read-Host '有证据支持的采用理由'
$Adoption = & $Python -m reviewctl adopt --workspace $Workspace --artifact $ArtifactId `
    --expected-hash $ExpectedHash --actor $Actor --reason $Reason | ConvertFrom-Json
$AdoptionExitCode = $LASTEXITCODE
if ($AdoptionExitCode -ne 0 -or $Adoption.status -notin @('succeeded', 'already_adopted')) {
    throw 'Outline adoption did not succeed; do not continue Writer.'
}
```

只有 adoption 成功后，使用同一 workspace 的 `resume` 继续 Stage 3 Writer 和后续请求；每个 Writer section、Validator 请求均受原有限预算、路由和闭包约束。`validation-status` 是只读检查；`validate` 会运行当前 ValidationExecutionService，可能发 Validator 请求：

```powershell
& $Python -m reviewctl resume --workspace $Workspace
& $Python -m reviewctl validation-status --workspace $Workspace
# 仅当 next-action/闭包确需执行新 validation 且预算与主机授权仍有效时：
& $Python -m reviewctl validate --workspace $Workspace
```

修复先生成 report-only `repair-plan`；审阅完整计划和目标 diff，得到所有者批准后才对精确计划运行 `repair-apply`。它产生隔离的派生产物，不直接使旧 canonical draft 变成当前版本。手工 proposal 必须是 typed 文件，并同时带 `--actor` 与 `--reason`。随后 `repair-promote --transaction ... --actor ... --reason ...` 重新运行当前 Validator 并仅在通过后晋升；如当前 Validator route 要求 host acknowledgement，必须使用同一 preflight policy 下已批准的 `--validator-host-ack-file`。

```powershell
& $Python -m reviewctl repair-plan --workspace $Workspace
$PlanId = Read-Host '已审阅并批准的 READY repair plan ID'
& $Python -m reviewctl repair-apply --workspace $Workspace --plan $PlanId
$TransactionId = Read-Host 'repair-apply 返回的准确 transaction ID'
$RepairActor = Read-Host '批准并执行修复晋升的实际人员'
$RepairReason = Read-Host '晋升和复检理由'
& $Python -m reviewctl repair-promote --workspace $Workspace --transaction $TransactionId `
    --actor $RepairActor --reason $RepairReason
& $Python -m reviewctl validation-status --workspace $Workspace
```

晋升后重新查看 `validation-status` 和 `current_artifact_set`；不能把之前的 clean 结果套到新修复版本上。

## 5. 同一 Registry revision 的可信导出

只有在所需 stage closure、 adoption、Writer 输出和当前 validation closure 都绑定到当前 `current_artifact_set` 后导出。`export` 会写导出包和取证证据，不是只读命令：

```powershell
& $Python -m reviewctl status --workspace $Workspace
& $Python -m reviewctl validation-status --workspace $Workspace
$Export = & $Python -m reviewctl export --workspace $Workspace | ConvertFrom-Json
$Export | ConvertTo-Json -Depth 64
if ($Export.bundle_path) { Get-FileHash -LiteralPath $Export.bundle_path -Algorithm SHA256 }
```

确认返回状态、bundle path、current set/validation provenance 和 ZIP SHA256。若之后采用、修复、晋升、重跑 Validator 或改变 source/config/current pointer，应把此前导出视为旧 revision 并重新导出。`canonical_unvalidated` 明确表示未做语义验证，不能描述成 Validator 通过；`untrusted` 不能发布为完成的综述。

本地 fixture、localhost transport 和合成 Provider 只证明相应的软件流程；不证明真实外部 Provider/backend、论文科学结论、真实规模验收或发布条件。配置里记录的 Provider/model/endpoint 及 route fingerprint 也不是独立的物理 backend 证明。

来源损坏、预算耗尽、回执不确定或运行中断时，先按[恢复流程](../../en/RECOVERY_RUNBOOK.md)读取 Registry、request audit、receipt ledger 和 next action。保留 PDF、Registry、回执及已消费账本；不要手改健康状态或重置额度、首次启动时间和 deadline。
