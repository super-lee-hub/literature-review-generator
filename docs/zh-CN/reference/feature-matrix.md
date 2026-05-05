# 功能实现状态矩阵

> 受众：维护者 / AI Agent / 贡献者。
> 本文档是内部状态文档，不是主要的终端用户指南。
> 来源：FEATURE_MATRIX.md（完整迁移）。

## 图例
- `implemented`：功能已完整实现并可正常使用
- `partial`：功能部分实现但未完全可用
- `legacy`：功能已实现但将在未来版本中弃用
- `planned`：功能已规划但尚未实现

## 核心功能

| 功能 | 状态 | 说明 | 备注 |
|------|------|------|------|
| JobWorkspace | implemented | 带产物追踪的 job workspace 管理 | 完全可用 |
| ArtifactRegistry | implemented | 用于追踪依赖关系的产物注册表 | 完全可用 |
| Config Compatibility | implemented | 配置文件兼容层 | 完全可用 |
| Review Draft v2 | implemented | 更新的综述草稿结构 | 完全可用 |
| Citation Manifest v2 | implemented | 结构化引用管理 | 完全可用 |
| Stage1 Multimodal Input | implemented | 阶段一多模态输入支持 | 完全可用 |
| Citation Object Main Chain | implemented | 引用对象作为主真相来源 | 完全可用 |
| Validation/Repair | implemented | 验证和修复管线 | 完全可用 |
| GUI Queue System | implemented | 工作流页面串行后台队列 | 完全可用 |
| AI-native Skill Entrypoint | implemented | 仓库本地 Codex / OMX skill 入口 | 完全可用 |
| Runtime Stage Trace | implemented | AI-native 运行时来源/追踪产物 | 完全可用 |
| Outline Review Compatibility | partial | 可选提纲审查/仲裁/采纳兼容面 | 显式手动兼容路径 |
| Zotero Integration | implemented | Zotero report 解析和库集成 | 完全可用 |
| PDF Extraction | implemented | 多后端 PDF 文本提取 | 完全可用 |
| AI Integration | implemented | OpenAI 兼容 API 集成 | 完全可用 |
| GUI Interface | implemented | 本地 GUI 工作流管理 | 完全可用 |
| CLI Interface | implemented | 命令行界面 | 完全可用 |

## 路线图

### P0：稳定性和真相对齐
- 修复 Windows pymupdf4llm/onnxruntime 访问冲突
- 统一 --zotero-report 和 --library-path 直接执行链
- 创建功能真相矩阵并更新文档

### P1：引用对象主链
- 让引用对象成为默认真相来源
- 扩展 review_draft_v2 块结构
- 更新 DOCX v2 路径以使用 manifest 参考文献

### P2：验证和修复
- 更新 ReviewValidator 输入结构
- 修改 SummaryRechecker 为 canonical-only
- 实现修复根因分类

### P3：队列产品化
- 扩展 QueueJobSpec/QueueJobRuntime
- 向 GUI 添加嵌入式工作流页面队列操作
- 移除公开 CLI 队列命令，保持 CLI 直接运行

### P4：大纲审查简化
- 保持 markdown 大纲生成为标准路径
- 避免声称 critique/arbitration/adopt 是默认工作流的一部分
- 将剩余的大纲审查辅助工具视为可选兼容代码

### P5：文档和 GUI 更新
- 更新 GUI 验证入口和配置文本
- 清理测试临时产物
- 生成新的真相来源文档
