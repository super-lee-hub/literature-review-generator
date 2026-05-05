# 架构总览与模块地图

> 受众：开发者、维护者、AI Agent。
> 来源：AGENTS.md §4, §8, §11, §12。

## 当前架构总览

```text
用户入口
├─ CLI: main.py -> dispatch_command() -> services/job_runner.py
└─ GUI: launch_gui.py -> gui/app.py -> services/workflow_facade.py -> services/job_runner.py

AI-native 入口
└─ Codex / OMX: .codex/skills/auto-generate-orchestrator/SKILL.md
   -> runtime/job_spec.py
   -> runtime/orchestrator.py
   -> services/job_runner.py / validator.py / workspace artifacts

运行时 / 工作区层
├─ services/job_workspace.py
├─ services/artifact_registry.py
├─ services/progress_state.py
├─ services/job_fingerprint.py
└─ services/queue_service.py

阶段一：输入、预处理、结构化摘要
├─ zotero_parser.py
├─ file_finder.py
├─ preprocess/service.py
├─ preprocess/visual_artifacts.py
├─ services/stage1_input_builder.py
├─ summary_schema.py
├─ services/paper_artifact.py
└─ services/summary_reuse.py

阶段二 / 三：大纲、综述、引用主链
├─ main.py / LiteratureReviewGenerator
├─ services/review_draft.py
├─ services/citation_manifest.py
├─ report_generator.py
└─ docx_writer.py

验证 / 修复层
├─ validator.py
├─ validation/review_validator.py
├─ validation/summary_recheck.py
├─ validation/repair_planner.py
├─ validation/repair_apply.py
└─ services/repair_integration.py

产品化 / 配置 / 扩展
├─ gui/app.py
├─ services/configuration_service.py
├─ services/environment_service.py
├─ free_mode/service.py
├─ free_mode/profile_manager.py
└─ rag/local_rag.py
```

## GUI 与 CLI 的关系

### 稳定事实

- CLI 入口仍然是 `python main.py ...`
- GUI 入口仍然是 `python launch_gui.py`
- GUI 没有自建完全独立引擎；GUI 和 CLI 共享底层 job request / workspace / artifact 逻辑，但只有 GUI 拥有用户可见的后台队列交互
- 仓库还有 **repo-local Codex skill** 这一条 AI-native 入口；它是加法面，不替代 GUI / CLI

### 当前执行链

- GUI 通过 `services/workflow_facade.py` 构造参数
- 实际执行由 `services/job_runner.py` 协调
- `main.py` 的 `dispatch_command()` 仍然是兼容入口，但真实运行已经越来越依赖 job workspace / artifact / resume 这一层
- AI-native skill 通过 `runtime/job_spec.py` + `runtime/orchestrator.py` 归一化输入，并在本地复用同一套 workspace / artifact / validation 基座

### 不要沿用的旧判断

- "GUI 只是薄壳，核心只有 main.py"
- "所有输出都在 `output/<project>/`"
- "Word/Excel 就是唯一主产物"

这些判断已经不够准确。

## 技术债与注意事项

当前仓库最重要的技术现状：

- `main.py` 依然很大，历史逻辑和新逻辑共存
- 项目名称尚未完全统一，仍残留 `llm_reviewer_generator`
- 有些旧帮助文本 / 老文档 / 迁移期说明会落后于当前 reality
- 兼容路径仍然存在，尤其是 outline adopt、部分 legacy output / citation fallback
- GUI 虽然已经产品化很多，但仍然共享历史主流程，不要假设所有页面文案都天然等于底层真实能力
- Word / Excel 是导出层，不是所有问题都应该从这些导出层倒推
- 看到老式 `output/<project>/` 内容时，先确认它是不是兼容指针而不是真实 workspace

## 模块地图：改哪里看哪里

如果未来任务是：

- **改用户文档分工**：`README.md`、`README.zh-CN.md`、`README.en.md`、`AGENTS.md`
- **改阶段一摘要结构**：`summary_schema.py`、`services/paper_artifact.py`、`report_generator.py`、相关 tests
- **改阶段一复用**：`services/summary_reuse.py`、`tests/test_summary_reuse.py`
- **改 workspace / output / resume**：`services/job_runner.py`、`services/job_workspace.py`、`services/artifact_registry.py`、`services/progress_state.py`
- **改 GUI 工作台**：`gui/app.py`、`services/configuration_service.py`、`tests/test_gui_playwright.py`
- **改 PDF 预处理**：`preprocess/service.py`、`rag/local_rag.py`、`tests/test_preprocess_service.py`
- **改 review draft / citation 主链**：`services/review_draft.py`、`services/citation_manifest.py`、`docx_writer.py`
- **改 validation / repair**：`validator.py`、`validation/*.py`、`services/repair_integration.py`
- **改 queue**：`services/queue_service.py`、`gui/app.py`、`tests/test_queue_service.py`
- **改 AI-native skill / runtime bridge**：`.codex/skills/auto-generate-orchestrator/SKILL.md`、`runtime/*.py`、`tests/test_runtime_*.py`
