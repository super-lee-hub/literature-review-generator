# 工作区布局与产物注册

> 受众：维护者、AI Agent。
> 来源：AGENTS.md 和[运行时真源](./truth-sources.md)。

## 当前真实输出目录

当前主输出位于 `output/<project_name>__<job_id>/`，典型结构：

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

## 兼容目录

`output/<project_name>/` 现在通常只保留指针（例如 `_latest_job.json`），不要再默认认为它是主产物目录。

## 预处理缓存

预处理缓存位于 `output/_preprocess_cache/`，常见缓存文件：

- `normalized.md`
- `plain_text.txt`
- `page_index.json`
- `chunks.json`
- `diagnostics.json`
- `structured.json`
- `prepare_manifest.json`

## Job Workspace 布局

真实产物必须只存在于 job workspace 内部：

```text
output/<project_name>__<job_id>/
├─ artifacts/
├─ checkpoints/
├─ logs/
├─ reports/
└─ artifact_registry.json
```

兼容指针目录：

```text
output/<project_name>/
└─ _latest_job.json
```

## 硬约束

- 禁止隐藏双写
- 除 `output/<project_name>/_latest_job.json` 外，任何代码路径不得将 summary / checkpoint / outline / review / report 写回 `output/<project_name>/`
- 真实产物必须只写一次，写入活跃 job workspace 内
- 下游代码必须从 job workspace 或 registry 读取持久产物

## 指针原子性契约

1. 在目标目录中写入临时文件
2. 刷新文件内容
3. `fsync` 临时文件
4. `rename` / `os.replace` 覆盖目标

任何非原子指针更新视为迁移 bug。

## 产物注册

`artifact_registry.json` 是 job workspace 内的中央产物注册表，追踪所有产物的依赖关系和版本。
