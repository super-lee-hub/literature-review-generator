# Prompt Inventory

当前仓库里的 prompt 分成两类：正在主流程使用的 prompt，以及历史保留的 prompt。

## 当前主流程在用

- `optimized_prompt_analyze_router.txt`
  阶段一文献分析主 prompt。当前 `main.py` 只走这一份。
- `prompt_system_analyze.txt`
  阶段一系统 prompt。
- `prompt_concept_analysis.txt`
  论文与背景概念关系分析。
- `prompt_prime_concept.txt`
  概念学习 / 概念预热。
- `prompt_synthesize_outline.txt`
  生成综述大纲。
- `prompt_continue_outline.txt`
  大纲续写。
- `optimized_prompt_synthesize_section.txt`
  章节正文主 synthesize prompt。
- `prompt_synthesize_section.txt`
  章节正文备用 prompt。
- `prompt_continue_section.txt`
  章节续写。
- `prompt_system_outline.txt`
  大纲阶段系统 prompt。
- `prompt_system_section.txt`
  章节阶段系统 prompt。
- `prompt_synthesize.txt`
  整体综述生成 prompt。
- `prompt_system_synthesize.txt`
  整体综述阶段系统 prompt。
- `prompt_validate_analysis_strict.txt`
  阶段一严格验证。
- `prompt_validate_claims_batch.txt`
  claim 批量核查。

## 历史保留 / 当前主流程未直接使用

- `optimized_prompt_analyze.txt`
  旧版高密度阶段一分析 prompt。它的“内容密度要求”已经部分并入 router 版。
- `optimized_prompt_analyze_v2.txt`
  过渡版阶段一分析 prompt。
- `prompt_analyze.txt`
  更早期的阶段一分析 prompt。

## 为什么会看起来重复

- 阶段一分析 prompt 这一组文件是同一任务在不同阶段的演化版本，不是当前同时并行使用。
- 现在真正生效的是 `optimized_prompt_analyze_router.txt`，其余 3 份主要用于回溯历史设计和人工对比。
- 章节、大纲、综述、验证这几组 prompt 也各自包含“主 prompt + system prompt + continue/fallback prompt”，所以文件名看起来会比较多。
