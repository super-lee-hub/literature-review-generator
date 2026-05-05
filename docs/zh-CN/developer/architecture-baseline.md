# 架构基线（历史参考）

> 最后更新：`2026-04-02`
> 受众：维护者 / AI Agent。
> 注意：本文件是迁移时期基线，不是当前运行时真相。当前运行时真相请参考 AGENTS.md 和 docs/ 中的运行时文档。

## 基线

- 仓库：`super-lee-hub/literature-review-generator`
- 分支：`main`
- 提交：`a3ba7ebfc10eaabda62d08ca3dfc47e7fafe2755`
- 本文档冻结范围：`Week 0` 及 `Week 1+` 迁移基线

## 当前真相来源（快照时）

快照时运行时真相分散在以下入口和输出文件中：

- `main.py` — 主编排中心、CLI 参数路由、输出路径决策、阶段 1/2/3 checkpoint 处理
- `validator.py` — legacy 阶段二验证入口
- `docx_writer.py` — legacy 综述渲染后端
- `services/workflow_facade.py` — GUI/CLI 兼容薄层
- `output/<project>/` — legacy 混合工作区

## 目标真相来源

迁移后，持久真相固定为 job workspace 产物：

- `review_draft.json`
- `citation_manifest.json`
- `paper_artifact.json`
- `visual_manifest.json`
- `outline.json`
- `artifact_registry.json`

这些文件是下游执行和恢复的唯一持久真相。兼容投影可能存在，但绝不是主真相来源。

## 兼容角色

- `main.py` — 兼容入口，必须停止积累新的长期领域逻辑
- `validator.py` — 兼容入口，必须委托给未来的 `validation/` 服务
- `docx_writer.py` — 仅渲染后端，不能决定未来的引用或参考文献真相
- `services/workflow_facade.py` — 迁移缓冲层，GUI 和 CLI 共用，必须保留到两个入口使用同一底层执行语义

## 硬约束

- 禁止隐藏双写
- 除 `output/<project_name>/_latest_job.json` 外，任何代码路径不得将 summary / checkpoint / outline / review / report 写回 `output/<project_name>/`
- 真实产物必须只写一次，写入活跃 job workspace 内
- 下游代码必须从 job workspace 或 registry 读取持久产物，不得读取 legacy 项目根副本

## 指针原子性契约

指针更新要求原子操作：

1. 在目标目录中写入临时文件
2. 刷新文件内容
3. `fsync` 临时文件
4. `rename` / `os.replace` 覆盖目标

任何非原子指针更新视为迁移 bug。

## 真相矩阵

| 领域 | 当前来源 | 目标来源 | 兼容投影 | 停止写入时间 |
|------|----------|----------|----------|-------------|
| Summaries | `*_summaries.json` | `paper_artifact.json` | `*_summaries.json` | Week 1 |
| Outline | `*_outline.md` | `outline.json` | `*_outline.md` | Week 5 |
| Review Draft | Checkpoint + Word | `review_draft_v2.json` | `*_review_checkpoint.json` | Week 3 |
| Citations | Word / regex | `citation_manifest_v2.json` | `citation_manifest_v1.json` | Week 3 |
| Validation Reports | Legacy validator | `validation/review_validator.py` outputs | TBD | Week 4 |
| Configuration | `Performance` section | `Validation` section | 双向同步 | Week 0 |
| Queue | N/A | `Queue` section | N/A | Week 5 |
| Stage1 Visual | N/A | `Stage1_Visual` section | N/A | Week 5 |
| Multimodal | N/A | `Multimodal` section | N/A | Week 5 |

## 停止写入时间线

- **Week 0**：冻结本文档；`Validation` 与 `Performance` 段双向同步
- **Week 1**：停止将真实产物写入 `output/<project_name>/`；仅保留 `_latest_job.json` 在项目根兼容目录
- **Week 2+**：仅在 job workspace 和 registry 中添加新的持久契约
