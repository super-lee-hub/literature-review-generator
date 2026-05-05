# 兼容性路径与弃用时间线

> 受众：维护者、AI Agent。
> 来源：TRUTH_SOURCES.md。

## 兼容性投影

### 字段兼容性
- **规范字段**：由 `summary_schema.py` 和规范阶段一摘要结构驱动
- **修复归属提示**：`validation/summary_recheck.py` 中的 `FIELD_OWNER_REGISTRY`
- **Legacy 字段**：仅在投影 / 归一化层中支持

### API 兼容性
- `Primary_Reader_API`：文献分析
- `Backup_Reader_API`：提取失败降级
- `Writer_API`：综述段落生成 / 重新生成
- `Outline_API`：大纲生成
- `Free_Mode_API`：自由模式规划
- `Validator_API`：综述验证

### 输入/输出兼容性
- PDF Folder 模式、Zotero 模式、GUI Queue 模式、AI-native 模式
- 主持久输出目录：`output/<project_name>__<job_id>/`
- 兼容指针目录：`output/<project_name>/`（仅 `_latest_job.json`）
- 预处理缓存：`output/_preprocess_cache/`

## 已弃用路径

### 阶段一
- 无规范 schema 的 legacy 摘要结构
- 基于正则的引用提取作为主要来源
- 无预处理验证的 OCR

### 阶段二
- 大纲的 auto-accept/auto-adopt
- 使用 `Writer_API` 生成大纲（应使用 `Outline_API`）

### 阶段三
- 无结构化引用的 APA 文内引用
- 无 `block_source` 和 `span_map` 的综述草稿

### 阶段四
- 基于摘要的参考文献（使用 manifest cited bibliography）
- 无 citation manifest 的 DOCX 生成

## 移除时间线

### Phase 1：当前版本 (v1.0)
- 所有已弃用路径仍作为降级可用
- 在元数据和日志中标记已弃用路径

### Phase 2：下一个小版本 (v1.1)
- 已弃用路径默认禁用，可通过配置重新启用
- 为已弃用路径使用添加警告消息

### Phase 3：下一个大版本 (v2.0)
- 已弃用路径完全移除
- 清理代码库并移除兼容层

## 关键实现说明

### 引用对象主链
- `citation_manifest_v3` 中的结构化引用是主要真相来源
- 基于正则的引用仅允许作为 legacy 降级
- 所有引用必须映射到规范论文键
- DOCX 参考文献仅包含实际被引用的条目

### 验证和修复
- `ReviewValidator` 使用 `review_draft + citation_manifest + preprocess/visual evidence + paper metadata`
- 修复根因分类：`citation_mapping_error`（manifest mapping + rerender）、`summary_drift`（targeted summary recheck）、`review_drift`（block/span patch）

### GUI 队列系统
- 默认队列策略：串行执行、失败继续、显式恢复、失败/取消 GUI job 重试
- CLI 和 AI-native 运行时是直接运行面，不暴露公共队列工作流

### 可选大纲审查兼容面
- `--outline-adopt` 是显式/手动兼容命令，不是默认工作流的一部分
