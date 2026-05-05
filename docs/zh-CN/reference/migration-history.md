# 迁移历史

> 受众：维护者 / AI Agent。
> 本文档记录 job-workspace 执行模型引入的迁移期行为和兼容性决策。
> 来源：MIGRATION_NOTES.md（完整迁移）。

## Week 1 迁移说明

- 旧项目输出仍然可以检测到，但不再是持久执行目标
- 真实产物现在位于 `output/<project_name>__<job_id>/`
- `output/<project_name>/` 仅保留 `_latest_job.json`

## 旧输出识别

Legacy 项目通过以下文件识别：
- `<project>_summaries.json`
- `<project>_checkpoint.json`
- `<project>_literature_review_outline.md`
- `<project>_review_checkpoint.json`
- `<project>_literature_review.docx`

## 恢复语义

- `strong_resumable`：可读的 summaries 产物存在 + `stage1_progress_snapshot.json` 存在 + 指纹包匹配当前请求
- `weak_resumable`：Summaries 存在，无兼容进度快照，用户应优先选择全新重新运行
- `non_resumable`：所需产物缺失或指纹不匹配

## summaries-only 旧状态

- Legacy `summaries-only` 状态被视为 `weak_resumable`
- 不能静默升级为强恢复

## Checkpoint 兼容

- Legacy `*_checkpoint.json` 在迁移期间保持可读
- 不再是主要恢复来源
- 主要恢复来源：workspace、progress snapshot、artifact registry、fingerprint bundle

## 需要全新重新运行的情况

- 输入源指纹变化
- 配置指纹变化
- 指针指向缺失的 workspace
- Summaries 不可读
- 进度快照过期或不匹配

## Direct PDF vs Zotero

两种模式必须在论文处理前归一化为共享源描述符：
- Direct PDF 模式从弱元数据和本地文件衍生标识开始
- Zotero 模式从结构化元数据和库支持标识开始
- 两者在下游处理前汇聚为 `SourcePaperDescriptor`

## Citation Manifest V2 (Week 6)

### 运行时真相来源升级

`citation_manifest_v2.json` 现在是主要引用真相来源，`citation_manifest_v1.json` 作为显式兼容投影保留。

**V2 结构：** `occurrences`、`clusters`、`bibliography`

**迁移路径：** 现有 v1 manifest 通过 `migrate_v1_to_v2()` 自动迁移。Validator 以 `occurrences` 为主要输入（降级到 `citations`）。

**文件位置：**
- 主：`output/<project>__<job_id>/citation_manifests/<project>_citation_manifest_v2.json`
- 兼容：`output/<project>__<job_id>/citation_manifests/<project>_citation_manifest_v1.json`
