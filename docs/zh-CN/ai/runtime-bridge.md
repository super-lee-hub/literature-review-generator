# AI 运行时桥接

> 受众：AI Agent、runtime 开发者。
> 来源：AGENTS.md §5.5, §6.4, §7；TRUTH_SOURCES.md。

## 验证 / 修复管线

项目已有单独的 validation / repair 管线：

- `validation_report`
- `repair_plan`
- `repair_apply_result`

用户可见入口仍以 `--validate-review` 为主，但内部已存在更细分的 evidence resolver、summary recheck、repair planner / apply 结构。

## 阶段四（验证 / 修复）真相来源

启用时会出现：

- `validation_report*.json`
- `repair_plan_*.json`
- `repair_apply_result_*.json`
- 以及相关 patch 记录

## Job Workspace、输出目录与缓存

### 当前真实输出目录

当前主输出位于 `output/<project_name>__<job_id>/`：

```text
output/<project_name>__<job_id>/
├─ artifacts/
│  ├─ <project>_summaries.json
│  ├─ <project>_summary_source_manifest.json
│  ├─ <project>_summary_reuse_report.json
│  ├─ <project>_literature_review_outline.md
│  ├─ paper_artifacts/
│  ├─ review_drafts/
│  ├─ citation_manifests/
│  └─ validation / repair 相关 JSON
├─ checkpoints/
├─ logs/
├─ reports/
└─ artifact_registry.json
```

### 兼容目录

`output/<project_name>/` 现在通常只保留指针（例如 `_latest_job.json`），不要默认认为它是主产物目录。

### 预处理缓存

预处理缓存位于 `output/_preprocess_cache/`，常见缓存文件包括：`normalized.md`、`plain_text.txt`、`page_index.json`、`chunks.json`、`diagnostics.json`、`structured.json`、`prepare_manifest.json`。

## AI-native Runtime Bridge

- `RuntimeJobSpec` 将 AI-native 请求适配为规范 `JobRunRequest`
- `AgentRuntimeBridge` 本地引导 workspace/latest-pointer 处理，并持久化 `source_bundle.json` + `runtime_stage_trace.json`
- 生成阶段可委托给子 agent，但 workspace/artifact/validation 转换保持本地和规范
- 此面是加法面：不替代正常的人类 CLI/GUI 入口

### Stage 7 产物

- `source_bundle.json`：归一化的 AI-native 输入/来源快照
- `runtime_stage_trace.json`：local-vs-subagent 阶段执行追踪
- 规范下游产物通过与 CLI/GUI 运行相同的 workspace/registry 基座持久化
