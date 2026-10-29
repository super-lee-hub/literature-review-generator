# Windows 操作手册

本手册是普通操作员从 PDF 文件夹创建任务，到采用 Outline、恢复、修复和导出的当前操作入口。公共 CLI 是 `reviewctl`；GUI 使用相同的 `RuntimeJobSpec`、Runner 和 Registry 契约。本文命令依据当前 parser 和 handler 核对，未在编写本文时启动 GUI、Runtime 或 Provider。

**2026-10-25 的入口状态：** `D:\auto-generate\reviewctl.bat` 与 `start_gui.bat` 的最终版本固定值尚未更新；目前目标工作区有未提交改动，稳定入口会被 DIRTY 检查拒绝。维护者必须先完成最终源码选择、干净提交、解释器与来源固定，以及实际启动验证。下文的执行命令以这些工作完成为前提；不要删除 DIRTY 检查或改走开发入口来绕过它。

当前本地模拟 Provider 的结果只证明对应工程流程；它不证明真实外部 Provider 可用、论文结论正确、500/600 篇验收完成或系统已达到发布条件。

## 1. 入口、配置与输入

在 PowerShell 中使用稳定入口。CLI 的 JSON 在标准输出，入口身份或诊断可能在标准错误；解析 JSON 时不要合并 `2>&1`。

```powershell
$EntryRoot = 'D:\auto-generate'
$Ctl = Join-Path $EntryRoot 'reviewctl.bat'
$Gui = Join-Path $EntryRoot 'start_gui.bat'
$Config = Join-Path $EntryRoot 'config.ini'

# 入口完成最终固定并通过启动验证后，任选 CLI 或 GUI。
& $Ctl doctor --config $Config
# & $Gui
```

稳定入口负责固定源码目录、Git 版本、解释器、配置和 GUI 端口。当前旧入口仍指向 Anaconda 3.11.7，受控执行环境是 F 工作区的 `.venv` Python 3.11.9；这项差异须由维护者在最终入口更新时解决，操作员不要自行混用解释器。`start_gui_dev.bat` 不是正常操作入口。

配置依据同一版本的 [`config.ini.example`](../../config.ini.example) 和[配置参考](reference/configuration.md)。已有 `config.ini` 时保留其中自定义设置；不要整份覆盖，更不要把实际密钥、`.env` 或完整私有配置提交进 Git。新安装仅在配置不存在时复制对应版本模板，然后填写自己的已批准服务地址、模型和密钥。

例如，模板的 Writer 配置使用下列字段；尖括号内容须由配置所有者替换。这些示例值不指向可调用服务，也不保证某个模型当前可用。

```ini
[Writer_API]
api_key = <your-own-key>
model = <approved-model-id>
api_base = https://your-approved-host.example/v1
endpoint_type = responses
provider_family = openai_responses
```

分别检查 `Primary_Reader_API`、`Backup_Reader_API`、`Outline_API`、`Writer_API`、`Free_Mode_API` 和 `Validator_API` 的实际路由；某些 Outline critic 会使用 `Free_Mode_API`，即使没有启用 Free Mode。不同服务的 `endpoint_type`、`provider_family` 与路径不能直接照抄 Writer 示例。另行检查解析器是否请求 MinerU 远程上传、是否允许本地回退，以及所有重试设置。

输入使用研究者已经持有且有权处理的 PDF 文件夹。研究者负责原文事实、研究归属和限定条件的确认。发现源事实错误时保留原始材料，通过受控的 source-correction 计划、独立检查、所有者采用和 typed reuse 流程产生派生版本；不要让 Writer 直接改写来源事实，也不要手改 Stage 1 summary、Registry 或证据 ID。

先做零网络检查：

```powershell
$Doctor = (& $Ctl doctor --config $Config) | ConvertFrom-Json
if (-not $Doctor.ok) { throw 'doctor 未通过；先阅读返回的诊断。' }

$Preflight = (& $Ctl preflight --config $Config --action run_all) | ConvertFrom-Json
if (-not $Preflight.ok) { throw 'preflight 未通过；不要开始 run。' }
$Preflight.provider_roles
$Preflight.external_host_policy | ConvertTo-Json -Depth 12
```

`doctor` 和 `preflight` 不访问网络。`preflight` 检查有效配置、凭据来源、可达角色和传输构造，不能证明网络、账户额度或模型实际可用。`micro-probe` 会发真实请求，不属于上述零调用检查；只有另行明确批准该服务和请求后才执行。

## 2. 建立新任务并取得实际计划

以下代码为新任务建立独立目录、输入 spec 和 queue 路径。所有路径使用绝对路径；JSON 写为无 BOM 的 UTF-8，兼容 Windows PowerShell 5.1 与 PowerShell 7。不要把旧任务复制到新目录后重置账本。

```powershell
function Write-Utf8Json($Path, $Value) {
    $Json = $Value | ConvertTo-Json -Depth 64
    [IO.File]::WriteAllText($Path, $Json, [Text.UTF8Encoding]::new($false))
}

$Actor = Read-Host '实际操作人或研究者标识'
$Reason = Read-Host '本次任务目的与已批准的处理范围'
$PdfFolder = (Resolve-Path -LiteralPath (Read-Host '已有 PDF 文件夹的绝对路径')).Path
if (-not (Test-Path -LiteralPath $PdfFolder -PathType Container)) {
    throw 'PDF 输入必须是目录。'
}
$Project = 'my_review'
$JobId = 'review-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
$Work = Join-Path $env:USERPROFILE ('review-runs\' + $JobId)
if (Test-Path -LiteralPath $Work) { throw '目录已存在，请使用新的任务目录。' }
[void] (New-Item -ItemType Directory -Path $Work)
$WS = Join-Path $Work ($Project + '__' + $JobId)
$SpecFile = Join-Path $Work 'runtime-job.json'
$Spec = [ordered]@{
    project_name = $Project
    job_id = $JobId
    config = $Config
    action = 'run_all'
    source = [ordered]@{
        mode = 'direct'
        pdf_folder = $PdfFolder
        zotero_report = ''
        library_path = ''
    }
    queue_file = (Join-Path $Work 'queue.json')
    workspace_path = $WS
    metadata = [ordered]@{
        audit_actor = $Actor
        audit_reason = $Reason
    }
}
```

这是 `RuntimeJobSpec`，其顶层没有 `schema_version`。不要增加 `approval_key`、`approved` 等未知字段；预算放在 `metadata.provider_budget`。需要特定 A/B 跨论文比较时，在启动前按 `ReviewIntent` 的实际论文键、claim、qualifier 和来源闭包填写 `metadata.review_intent`，不能用临时编造的 A/B 名称替代实际绑定。

若 `external_host_policy.required_hosts` 非空，先逐项审核实际服务主机及上传范围。以下分支只有操作员明确确认后才建立声明，保留精确 host 集合和路由指纹。预算批准与外部主机确认是两件事。

```powershell
$Hosts = @($Preflight.external_host_policy.required_hosts)
if ($Hosts.Count -gt 0) {
    $Preflight.external_host_policy | ConvertTo-Json -Depth 12
    if ((Read-Host '所有者已批准向上述精确主机发送本任务允许的数据？输入 APPROVE 才继续') -cne 'APPROVE') {
        throw '未获得该外部路由的明确授权。'
    }
    $AckHours = [double] (Read-Host '已批准主机声明有效小时数，须覆盖计划运行期且不超过 168 小时')
    if ([double]::IsNaN($AckHours) -or [double]::IsInfinity($AckHours) -or $AckHours -le 0 -or $AckHours -gt 168) {
        throw '声明有效期必须大于零且不超过七天。'
    }
    $Issued = [DateTimeOffset]::UtcNow
    $Spec.metadata.external_host_acknowledgement = [ordered]@{
        schema_version = 'external-host-acknowledgement-v2'
        version = 2
        acknowledged = $true
        hosts = $Hosts
        route_fingerprint = $Preflight.external_host_policy.route_fingerprint
        issued_at = $Issued.ToString('o')
        expires_at = $Issued.AddHours($AckHours).ToString('o')
    }
}
Write-Utf8Json $SpecFile $Spec
$InitialPlan = (& $Ctl plan --spec $SpecFile) | ConvertFrom-Json
Write-Utf8Json (Join-Path $Work 'plan-initial.json') $InitialPlan
$InitialPlan | ConvertTo-Json -Depth 64
if ($InitialPlan.status -ne 'planned') { throw '计划未建立；先阅读 error/reason。' }
```

没有显式预算的第一次 `plan` 可能显示文档化的有限默认值；这里只用它读取实际内容与路由的计划，不据此执行。主机声明要求完整、无重复的精确主机集合、当前 route fingerprint 和有效时间；更换路由、配置或声明过期后不能沿用旧声明。官方服务不一定需要 gateway 声明，但仍须有处理数据和支出的真实授权。不要修改已启动任务的 spec 或配置来刷新授权。

重点阅读 `full_stage_request_plan` 的阶段请求、已知下界、未知暴露、`aggregate_budget_source`、`budget_status` 和阻断原因。`unknown` 不是零；已知下界也不是完整上界。Outline 输出会决定后续 Writer/Validator 请求，最终物化时仍要逐请求准入。`chunk-plan` 只覆盖其声明的 Outline 规划范围，不能替代端到端预算。

## 3. 明确批准有限预算，再运行

所有者结合实际计划、允许的费用与时间，选择下列四个有限上限。不要按“篇数乘任意常数”套用旧预算，也不要把测试的停止上限当成必需调用数。总输出 token 上限不是货币账单；未知价格不能当作免费。

```powershell
$Budget = [ordered]@{
    schema_version = 'provider-aggregate-budget-contract-v2'
    max_provider_calls_total = [long] (Read-Host '根据实际计划批准的总物理调用上限，正整数')
    max_output_tokens_total = [long] (Read-Host '批准的全部尝试累计输出 token 上限，正整数')
    max_retry_attempts_total = [long] (Read-Host '批准的累计重试上限，非负整数')
    max_wall_seconds = [double] (Read-Host '批准的运行墙钟秒数，有限正数')
}
$Spec.metadata.provider_budget = $Budget
Write-Utf8Json (Join-Path $Work 'approved-budget.json') $Budget
Write-Utf8Json $SpecFile $Spec
$Plan = (& $Ctl plan --spec $SpecFile) | ConvertFrom-Json
Write-Utf8Json (Join-Path $Work 'plan-approved.json') $Plan
$Plan | ConvertTo-Json -Depth 64
if ($Plan.status -ne 'planned' -or $Plan.full_stage_request_plan.status -eq 'blocked_before_request_inventory') {
    throw '计划或请求清单被阻断；不要执行 run。'
}
```

严格 V2 接受调用、输出和重试上限为非负整数，墙钟为有限非负数；零表示真实零额度，不表示无限。上面的示例用于有实际 Provider 工作的任务，因此选择足够的正调用、输出和时间额度。未知键或无效数值会拒绝。检查配置的传输重试、Reader 恢复、Outline/node/stage 重试是否与预算一致。预算不足应在启动前重新审阅内容范围和授权，不能削掉必要比较、qualifier 或来源闭包来让数字通过。

普通 spec 计划的 `aggregate_budget_source` 为 `explicit_runtime_spec`。当前 `ready_for_provider_admission` 还要求已绑定 acceptance-run authority，因此普通 `plan` 中该字段可能为 `false`；不要伪造 acceptance 环境变量或审批键来使它变为 `true`。必须同时检查实际阻断项、`run_resource_limits_finite` 和已知/未知请求暴露。启动时持久化的 run envelope 及后续 JIT reservation 才约束实际请求。

确认最后一次计划、准确输入和有限预算已得到所有者批准后执行：

```powershell
$Run = (& $Ctl run --spec $SpecFile) | ConvertFrom-Json
$RunExit = $LASTEXITCODE
Write-Utf8Json (Join-Path $Work 'run-result.json') $Run
$Run | ConvertTo-Json -Depth 64
if ($Run.workspace_path) { $WS = $Run.workspace_path }
```

`run_all` 在 validation 启用时请求 analyze、Outline v3、review、validate。不要在另一个窗口同时对同一 workspace 执行 run/resume/retry；attempt/queue lease 和 publication fence 会限制并发发布。启动后保持原 PDF、配置、spec 及引用的外部 reuse authority 可用且字节不变；Registry 由 Runtime 管理，操作员不得手改。

`run`/`resume` 只有 `job_status=completed` 且 `completion_status=complete` 才返回退出码 0。等待人工采用 Outline 可能返回非零，这不等于可以盲目重跑。下一步读 JSON：

```powershell
& $Ctl status --workspace $WS
& $Ctl next-action --workspace $WS
& $Ctl inspect --workspace $WS
```

## 4. 审阅并显式采用 Outline

查看 `inspect` 返回的实际 artifact，不按最新文件时间选候选。下面只定位 canonical final outline 的逻辑记录；先阅读它的内容、coverage audit、stage health 和 critique，再决定是否采用。

```powershell
$Inspection = (& $Ctl inspect --workspace $WS) | ConvertFrom-Json
$Final = @($Inspection.artifacts | Where-Object {
    $_.artifact_id -eq 'outline-v3:final_outline' -and
    $_.artifact_type -eq 'final_outline' -and
    $_.artifact_version -eq 'v3' -and $_.status -eq 'ready'
})
if ($Final.Count -ne 1) { throw '没有唯一 READY final outline；先处理 next-action 和阻断原因。' }
$Final[0] | ConvertTo-Json -Depth 16
Get-Content -LiteralPath $Final[0].path -Raw -Encoding UTF8

# 审阅通过后使用 Registry 返回的精确 ID/hash；采用命令仍会重新验证所有门禁。
$AdoptionReason = Read-Host '实际审阅结论与采用理由'
$Adoption = (& $Ctl adopt --workspace $WS --artifact $Final[0].artifact_id `
    --expected-hash $Final[0].content_hash --actor $Actor --reason $AdoptionReason) | ConvertFrom-Json
$Adoption | ConvertTo-Json -Depth 32
if ($Adoption.status -notin @('succeeded', 'already_adopted')) { throw '采用未通过；不要继续 Writer。' }
& $Ctl resume --workspace $WS
```

仅有文件或 READY 标签不够：采用要求精确哈希、类型/版本、typed dependencies、coverage、stage health 与 blocking critique 门禁全部通过。中间 candidate 不会自动成为正式 Outline。若采用被阻断，保留诊断；不要替换 ID、手改 audit 或跳过 critique。

## 5. Validation、修复审批与晋升

先读现状，再决定是否明确执行一次新的 Validation。`validation-status` 是只读检查；`validate` 会运行当前 `ValidationExecutionService`、可能调用 Provider，并持久化新 attempt、receipt 和依赖闭包。

```powershell
& $Ctl validation-status --workspace $WS
# 仅在 next-action/审阅结论要求新验证且现有预算、路由授权仍有效时执行：
& $Ctl validate --workspace $WS
$Repair = (& $Ctl repair-plan --workspace $WS) | ConvertFrom-Json
$Repair | ConvertTo-Json -Depth 32
```

如果独立 `validate` 或 `repair-promote` 需要第三方 Validator 主机声明，先针对 `preflight --config $Config --action validate_review` 返回的精确 policy 取得所有者确认，再按第 2 节的 v2 schema 保存声明文件，并在相应命令增加 `--validator-host-ack-file <文件绝对路径>`。全任务的 route fingerprint 不能假定适用于 Validator-only 路由。该文件不是密钥，也不能替代有限预算。

`repair-plan` 会返回已有计划，或创建 report-only 计划；它不修改 canonical 正文。新建计划通常返回 `plan_id`、`artifact_id`、`path`；已有计划可能返回 `plans` 数组。审阅实际记录及计划内容后，从返回值中选择准确的 plan ID，不能用文件名猜测：

```powershell
$PlanId = Read-Host '已审阅并批准的 repair plan ID 或对应 artifact_id'
# 只有计划本身标记 auto_apply_safe 且全部门禁通过，才生成 quarantined 派生版本。
$Applied = (& $Ctl repair-apply --workspace $WS --plan $PlanId) | ConvertFrom-Json
$Applied | ConvertTo-Json -Depth 32
```

默认 `report_only` 计划会拒绝上述自动应用；不能直接改计划 policy 来解锁。需要人工修改时，应由有权审核的人提供该版本要求的 typed manual proposal，再调用：

```powershell
$ProposalFile = (Resolve-Path -LiteralPath (Read-Host '已审核 typed manual proposal JSON 的路径')).Path
$RepairReason = Read-Host '人工修复的实际理由'
$Applied = (& $Ctl repair-apply --workspace $WS --plan $PlanId `
    --manual-proposal-file $ProposalFile --actor $Actor --reason $RepairReason) | ConvertFrom-Json
$Applied | ConvertTo-Json -Depth 32
```

CLI 要求 `--manual-proposal-file` 与非空 actor/reason 配套；只给 actor/reason 不会把 report-only 计划变成可应用计划。人工 proposal 仍须绑定当前 draft、manifest、plan 和 validation evidence；它不授权修改原文事实、论文限定条件或来源归属。

审阅 quarantined 派生正文、引文和差异，确认其符合实际证据后，使用应用结果中实际生成的 transaction ID 晋升：

```powershell
$TransactionId = Read-Host '已审阅 quarantined 修复的 transaction_id'
$PromotionReason = Read-Host '实际复核结论与晋升理由'
$Promotion = (& $Ctl repair-promote --workspace $WS --transaction $TransactionId `
    --actor $Actor --reason $PromotionReason) | ConvertFrom-Json
$Promotion | ConvertTo-Json -Depth 32
if ($Promotion.status -notin @('promoted', 'already_promoted')) { throw '晋升未通过；保留 quarantined 版本和诊断。' }
& $Ctl validation-status --workspace $WS
& $Ctl resume --workspace $WS
```

晋升会经当前 service 重新验证，可能发 Validator 请求；只有 durable revalidation 与完整闭包通过，才发布不可变的新版本并推进 current pointer。原 canonical 文件不会被原地覆盖。修复应用成功不是晋升成功，更不等于整个任务完成。

## 6. 恢复、暂停、取消和过期预算

普通恢复沿用持久化身份、来源和预算。它只复用经 Registry 验证的阶段，尚未完成的部分仍可能发请求。

```powershell
& $Ctl next-action --workspace $WS
& $Ctl reconcile --workspace $WS --dry-run
& $Ctl resume --workspace $WS

# 明确需要暂停或取消时，分别选择其一；不要作为常规恢复步骤顺序执行。
# & $Ctl pause --workspace $WS --reason '实际暂停理由'
# & $Ctl cancel --workspace $WS --reason '实际取消理由'
```

`cancel` 写入协作式取消请求，不杀进程；Worker 在安全检查点观察后结束。请求返回 `requested` 不是终态，应等实际 `cancelled`/terminal 证据。恢复不会重置首次启动时间、消耗或原绝对截止时间。`retry-node --node <实际失败节点 ID>` 只用于 `next-action` 明确建议、绑定和预算仍有效的 Outline v3 节点恢复，不能作为任意重跑开关。

**过期后的独立下游授权是严格受限分支。** 当前 `authorize-downstream` 只支持普通任务的原 V5 账本已经过期、没有未结 reservation、Stage 1/Outline/采用闭包完整且 Writer/Validator 全部为 loopback 路由的情况。它不支持外部服务续接，也不复活历史付费 pilot。

新授权文件使用第 3 节的严格 V2 schema，但数字表示本次**额外允许**的调用、输出和重试；每项不得超过原批准总额扣除已用后的余额。新的有限墙钟属于独立 grant，不延长原 deadline。以下文件必须事先由真实授权人根据剩余额度批准，不能复制旧预算当作全新额度：

```powershell
$AdditionalBudgetFile = (Resolve-Path -LiteralPath (Read-Host '已明确批准的额外有限 V2 budget JSON 路径')).Path
$AuthorizationReference = Read-Host '真实授权消息或变更记录的引用'
$ContinuationReason = Read-Host '仅续接 Writer/Validator 的实际理由'
$Grant = (& $Ctl authorize-downstream --workspace $WS --budget-file $AdditionalBudgetFile `
    --actor $Actor --reason $ContinuationReason `
    --authorization-reference $AuthorizationReference) | ConvertFrom-Json
$Grant | ConvertTo-Json -Depth 32
if ($Grant.status -ne 'authorized') { throw '未建立有效下游 grant；不要 resume。' }
& $Ctl resume --workspace $WS
```

命令只准备 append-only grant；随后普通 `resume` 负责执行。检查返回的 `carried_usage`、新 `run_id`、`provider_budget_state_path`、`absolute_deadline_epoch` 和允许阶段。旧 ledger、identity、spec、envelope、首次启动和 deadline 保持原字节；新账本只记录新增消费，Reader/Outline POST 仍禁止。已有 grant 不能通过更换理由或预算为同一旧父账本分叉。若条件不满足，保留阻断信息，不改旧账本、不换 job ID 偷渡恢复，也不重设历史 pilot。

## 7. 完成检查与导出

```powershell
$State = (& $Ctl status --workspace $WS) | ConvertFrom-Json
$State | ConvertTo-Json -Depth 32
& $Ctl validation-status --workspace $WS
$Export = (& $Ctl export --workspace $WS) | ConvertFrom-Json
$Export | ConvertTo-Json -Depth 32
if ($Export.status -ne 'canonical_verified' -or -not $Export.bundle_path) {
    throw '未获得 canonical_verified 导出；阅读导出的闭包和阻断原因。'
}
$Bundle = $Export.bundle_path
Get-FileHash -LiteralPath $Bundle -Algorithm SHA256
```

`export` 会写导出包和取证证据，不是只读命令。退出码 0 本身不证明可信；检查 JSON 的 status、非空路径/ID、current artifact set、completion 和 validation closure。`untrusted` 不能当成功包；canonical 注册失败会返回空路径/ID，并清除临时 ZIP。

若明确配置 validation optional 且禁用，typed not-requested policy 和完整闭包可能允许 `canonical_unvalidated`；它表示语义验证未执行，不能按 `canonical_verified` 验收。本节脚本有意只接收经过验证的状态。

在没有任何源、配置、current pointer 或 validation 变化时，可以重复导出并检查一致性：

```powershell
$FirstHash = (Get-FileHash -LiteralPath $Bundle -Algorithm SHA256).Hash
$Again = (& $Ctl export --workspace $WS) | ConvertFrom-Json
if ($Again.status -ne 'canonical_verified' -or -not $Again.bundle_path) {
    throw '重复导出未通过可信闭包检查。'
}
$AgainHash = (Get-FileHash -LiteralPath $Again.bundle_path -Algorithm SHA256).Hash
if ($FirstHash -cne $AgainHash) { throw '重复导出字节不同；保留两次结果检查身份和证据变化。' }
```

保留完整任务 workspace、原 PDF、被引用的外部 summary/receipt authority、配置的安全备份和导出包。只保存 DOCX 或单个 summary JSON 不能恢复 Registry 来源链。恢复包仍需在受控位置做实际 restore/readback；ZIP 存在不代表恢复已经验证。不要在验收前移动、清理或删除依赖文件。

## 8. 常见阻断与命令影响

| 现象 | 下一步 |
| --- | --- |
| 稳定入口报告 DIRTY、版本、来源或解释器不匹配 | 停止启动；由维护者完成干净源码与入口固定。不要关闭 guard。 |
| `doctor`/`preflight` 的 `ok=false` | 按 error/route/credential provenance 修正未启动任务的配置；不要输出密钥。零网络通过后仍不代表真实服务可用。 |
| `plan` 的 inventory blocked 或 unknown exposure | 阅读具体 reason，保留未知项；不能把未知记成零，也不能仅看退出码 0。 |
| 等待采用 final Outline / needs_review | 走第 4 节实际审阅和精确 hash 的采用操作，然后恢复。 |
| 缺失、stale、foreign、hash/type/version/typed edge 不匹配 | 恢复准确原文件或走受控派生流程；不要手补 READY、伪造 receipt 或删除失败证据。 |
| 网络错误、有限预算不足或 deadline 过期 | 先读 `next-action` 与账本状态；不隐式重试、不清零、不延时。仅符合第 6 节全部条件时建立独立有限下游 grant。 |
| Validation findings / repair blocked | 先 report-only 计划和人工审阅，再走受控应用/晋升；不能把 findings 擅自解释为 clean。 |
| 有 DOCX 但 export untrusted / completion incomplete | 以 current set、规范 JobOutcome 和完整闭包为准；单个磁盘文件不能证明完成。 |
| CLI 返回 `status=error` | 保存 `error_type`、`error`、命令和任务路径；不要分享密钥。该错误外壳中的 `read_only` 不是此前所有步骤零副作用的证明。 |

| 命令 | 主要影响 |
| --- | --- |
| `doctor`、`preflight`、`plan`、`status`、`inspect`、`next-action`、`validation-status`、`reconcile --dry-run` | 零 Provider 请求的检查/规划；大任务的完整来源闭包检查仍可能耗时，避免并发轮询。 |
| `run`、`resume`、`retry-node`、`validate`、`repair-promote` | 执行或恢复工作，可能发请求；均受相应预算、路由和来源门禁约束。 |
| `adopt`、`repair-plan`、`repair-apply`、`authorize-downstream`、`pause`、`cancel` | 写采用、计划、派生、授权或控制状态；不等于已经完成后续执行。 |
| `export`、`attest` | 写导出或取证结果；不能当只读命令。 |
| `micro-probe` | 真实最小 Provider 请求，需明确服务/支出授权。 |
| `config-migrate --dry-run` / `config-migrate` | 前者检查迁移，后者修改配置并按默认策略备份；不在运行中迁移。 |

## 9. 不可绕过的验收依据

完成以 Registry 的规范 `job_outcome` 和 `current-artifact-set:pointer` 解析出的原子 current set 为准；固定路径投影、历史 READY 文件、手写报告和候选 Outline 都不能代替 authority。review draft v3、citation manifest v3、DOCX v1、条件化 validation evidence 和 provider receipt closure 必须绑定同一当前版本。

有模型调用的 stage 必须具有实际 receipt ledger 和精确 call set；零调用复用也需要有效的 expected graph 与 typed source evidence，不能伪造空 ledger。Stage 1 reuse 要验证真实 PDF 字节及 summary/preprocess/input/prompt/provider/model/schema/visual 身份，引用的外部 authority 和 blob 必须持续可用；当前 snapshot 不是自身的来源证明。

publication、queue lease generation/fence、采用、修复晋升和导出均继续 fail-closed。不要手改 Registry/Stage Health、伪造收据、跳过来源闭包、恢复 Outline v2 或旧 `main.py` 直接 flag 流程。执行路线与故障细节见[当前真相来源](runtime/truth-sources.md)、[工作区布局](runtime/workspace-layout.md)、[信任模型](ARTIFACT_TRUST_MODEL.md)和[恢复手册](RECOVERY_RUNBOOK.md)。

本文的命令、字段和状态依据 `reviewctl.py`、`runtime/control_plane.py`、`runtime/job_spec.py`、`runtime/downstream_continuation.py`、`runtime/trust_admission.py`、`validation/repair_transaction.py` 与 `runtime/export_bundle.py` 静态核对。真实稳定入口启动、GUI 操作、外部 Provider、完整规模矩阵、恢复包 readback 和最终同版本 CI 仍须分别留存执行证据。
