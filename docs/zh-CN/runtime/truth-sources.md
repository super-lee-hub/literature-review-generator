# 真源体系与数据契约

> 受众：维护者、AI Agent。
> 来源：TRUTH_SOURCES.md；AGENTS.md §5-7。

本文档定义 auto-generate 项目各阶段的规范真相来源、数据契约和兼容性投影。

## 主真相来源

### 阶段一：论文分析
- **主真相来源**：canonical `*_summaries.json`
- **伴随持久产物**：`paper_artifact.json`（workspace 路径活跃时）
- **降级**：legacy summary 投影归一化到规范 summary schema
- **关键产物**：`*_summaries.json`（规范摘要结构）、`paper_artifact.json`（持久论文分析记录）、`*_analyzed_papers.xlsx`（导出物，非真相来源）

canonical summary 核心块：`routing`、`paper_metadata`、`core_analysis`、`specialized_details`、`quality_audit`

### 阶段二：大纲生成
- **主真相来源**：已注册的 markdown outline 产物 `*_literature_review_outline.md`
- **降级**：workspace/registry 产物不可用时使用 legacy output-folder markdown outline
- **关键产物**：`*_literature_review_outline.md`（综述生成使用的下游大纲）

### 阶段三：综述草稿
- **主真相来源**：`*_review_draft_v2.json` + `*_citation_manifest_v3.json`
- **降级**：Legacy review draft 结构（元数据标记为 legacy）
- **关键产物**：`*_review_draft_v2.json`（含 block 结构、`block_source`、`span_map`、持久 section context）、`*_citation_manifest_v3.json`（结构化引用）

### 阶段四：DOCX 生成
- **主真相来源**：`*_review_draft_v2.json` + `*_citation_manifest_v3.json`（仅引用参考文献）
- **降级**：Legacy summary-based bibliography（仅显式 legacy 模式）

### 阶段五：验证和修复
- **主真相来源**：`validation_report.json` + `repair_plan.json` + `repair_apply_result.json`
- **关键产物**：`validation_report.json`、`repair_plan.json`、`repair_apply_result.json`、`applied_patch_*.json`

### 阶段六：GUI 队列系统
- **主真相来源**：GUI 内部 `queue.json` 及不可变工作流提交快照
- **关键产物**：`queue.json`、`QueueJobSpec`、`QueueJobRuntime`

### 阶段七：AI-native 运行时桥接
- **主真相来源**：活跃 job workspace + artifact registry，由 `RuntimeJobSpec` 和 `AgentRuntimeBridge` 驱动
- **关键产物**：`source_bundle.json`、`runtime_stage_trace.json`

## 当前真实主链

### 输入模式
- PDF folder 模式：直接扫描文件夹中的 PDF
- Zotero 模式：通过 `Paths.zotero_report` + `Paths.library_path` 定位文献与附件

### 阶段一链路
1. 收集源论文描述 → 2. 解析并定位 PDF → 3. 预处理层 → 4. 构建 stage1 输入 → 5. Reader API 生成结构化摘要 → 6. 归一化到 canonical summary schema → 7. 写入 `*_summaries.json` → 8. 写入 `paper_artifact` → 9. 输出 Excel

### 阶段二链路
主输出：`*_literature_review_outline.md`，默认 API：`Outline_API`

### 阶段三链路
`review_draft_v2` → `citation_manifest_v3` → DOCX。`review_draft_v2 + citation_manifest_v3` 才是阶段三更重要的结构化真相来源，`docx` 是最终导出物。

### 验证 / 修复链路
独立管线：`validation_report` → `repair_plan` → `repair_apply_result`

## 数据契约

### 阶段一
- 主真相：canonical `*_summaries.json`
- 伴随 durable artifact：`paper_artifacts/*.json`
- 结构事实来源：`summary_schema.py`

### 阶段二
- 主真相：`*_literature_review_outline.md`

### 阶段三
- 主真相：`review_drafts/*_review_draft_v2.json`
- 引用主真相：`citation_manifests/*_citation_manifest_v3.json`
