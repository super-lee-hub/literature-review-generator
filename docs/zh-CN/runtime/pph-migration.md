# Prototype 与 PPH 迁移边界

当前 `codex/platform-hardening-outline-v3` 树中没有 `pph_*.py` 生产脚本。工作区中若有未跟踪的 PPH ZIP，它们属于用户输入，刻意不加入暂存、不导入、也不由 runtime 执行。

未来可复用能力应进入通用的 `ValidationClosureService`、`ExportBundleService`、声明式 review-batch 或 corpus patch transaction。历史或项目专用取证工具必须放入明确隔离的 `tools/legacy_forensics/`，不得进入 CLI/GUI/runtime 主路径，并声明只读或 quarantine 契约。

不得把实验分支整体复制到生产。迁移前先定义通用契约、增加聚焦测试、通过 `ArtifactRegistry` 注册输出并记录兼容投影。任何原地编辑 Registry、Stage Health、canonical draft、manifest 或 DOCX 的脚本都不是批准的迁移。
