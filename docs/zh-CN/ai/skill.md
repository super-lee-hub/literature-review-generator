# Codex/OMX Skill 文档

> 受众：AI Agent、Codex/OMX 用户。
> 来源：`.codex/skills/auto-generate-orchestrator/SKILL.md`

## 意图

- 在 CLI 和 GUI 之外增加第三条 AI-native 入口
- 复用当前持久基座（`services/job_runner.py`、`services/job_workspace.py`、`services/artifact_registry.py`、`services/progress_state.py`、`validator.py`）
- 保持确定性生命周期 / 持久化 / 渲染 / 验证转换在本地执行
- 通过子 agent 路由生成阶段，而不是依赖 legacy CLI 或外部 API 包装

## 规范约束

1. `services.job_runner.JobRunRequest` 保持为规范请求模型
2. CLI 和 GUI 保持为一等人类接口；不要替代它们
3. AI 模式是加法面，MVP 阶段不进入队列，但必须保持 workspace 兼容
4. 规范下游产物保持不变：summaries、markdown outline、`review_draft_v2`、`citation_manifest_v3`、docx、validation/repair 产物

## 主要运行时辅助模块

- `runtime.job_spec.RuntimeJobSpec`
- `runtime.orchestrator.AgentRuntimeBridge`
- `runtime.source_intake.*`
- `runtime.subagent_policy.*`
- `runtime.stage_contracts.*`
- `runtime.lifecycle.*`

## 预期操作模式

1. 将 AI 输入归一化为 `RuntimeJobSpec`
2. 编译为规范 `JobRunRequest`
3. 本地构建 source intake bundle
4. 本地引导 workspace / registry / resume 状态
5. 将生成阶段委托给子 agent：阶段一分析、阶段二提纲、阶段三综述
6. 通过现有规范产物辅助模块持久化输出
7. 通过现有验证接缝本地运行验证
8. 注册 runtime stage trace 产物，使执行模式可观测

## 硬禁止

- 不要把 `python main.py ...` 作为规范 AI 运行时
- 不要引入第二个平行的请求模型
- 不要绕过 latest-pointer / artifact-registry / resume-state 行为
- 不要用替代规范 schema 替换 `review_draft_v2` / `citation_manifest_v3`
- 当更丰富的产物证据存在时，不要把仅摘要级别的证据当作验证真相
