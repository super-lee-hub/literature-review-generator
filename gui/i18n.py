"""Lightweight GUI translations for the local NiceGUI workspace."""

from __future__ import annotations

from typing import Dict


LANGUAGE_OPTIONS: Dict[str, str] = {
    "zh-CN": "中文",
    "en": "English",
}


ACTION_LABELS: Dict[str, Dict[str, str]] = {
    "zh-CN": {
        "analyze": "文献分析",
        "outline": "大纲生成",
        "review": "全文生成",
        "run_all": "一键运行",
        "validate": "综述验证",
        "retry_failed": "失败重试",
    },
    "en": {
        "analyze": "literature analysis",
        "outline": "outline generation",
        "review": "full review generation",
        "run_all": "run all",
        "validate": "review validation",
        "retry_failed": "retry failed papers",
    },
}


TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "en": {
        "开始": "Start",
        "总览": "Overview",
        "项目入口与推荐流程": "Project entry and recommended flow",
        "核心工作台": "Workspace",
        "分析、写作与一键运行": "Analysis, drafting, and run-all",
        "配置": "Setup",
        "环境与路径": "Environment & Paths",
        "首次使用和基础 setup": "First-time setup and basic paths",
        "API 与模型": "APIs & Models",
        "阅读、写作、大纲和验证模型": "Reader, writer, outline, and validator models",
        "性能与预处理": "Performance & Preprocessing",
        "并发、OCR、缓存与 RAG": "Concurrency, OCR, cache, and RAG",
        "结果": "Outputs",
        "日志与产物": "Logs & Outputs",
        "查看状态、日志和输出目录": "View status, logs, and output folders",
        "使用引导": "Guide",
        "给第一次使用的人看的说明": "Notes for first-time users",
        "暂无日志文件。": "No log files yet.",
        "无法读取日志：{exc}": "Unable to read the log: {exc}",
        "路径为空。": "The path is empty.",
        "路径不存在：{target}": "Path does not exist: {target}",
        "未选择任何路径。": "No path was selected.",
        "已选择路径：{target}": "Selected path: {target}",
        "工作台已就绪。建议先进入“环境与路径”完成 setup，再回到“核心工作台”运行流程。":
            "Workspace is ready. Start with Environment & Paths, then return to Workspace to run your workflow.",
        "配置已保存到 {config_path} 和 {env_path}":
            "Configuration has been saved to {config_path} and {env_path}.",
        "配置已保存。": "Configuration saved.",
        "请先填写项目名。": "Please fill in the project name first.",
        "请填写 PDF 文件夹，或先在“环境与路径”页面填写 Zotero 报告路径。":
            "Please provide a PDF folder, or set the Zotero report path in Environment & Paths first.",
        "正在执行 {action_label}，请稍候……": "Running {action_label}. Please wait...",
        "{action_label} 已执行完成。": "{action_label} completed.",
        "{action_label} 执行失败：{reason}": "{action_label} failed: {reason}",
        "界面语言已切换。": "Interface language switched.",
        "目录": "Navigation",
        "AI 文献综述生成器": "AI Literature Review Generator",
        "保存配置": "Save Config",
        "打开输出": "Open Output",
        "打开日志": "Open Logs",
        "语言": "Language",
        "搜索功能": "Search Features",
        "例如：大纲、OCR、日志、自由模式": "Try: outline, OCR, logs, free mode",
        "搜索": "Search",
        "请先输入想找的功能。": "Please type a feature to search for.",
        "已跳转到 {destination}": "Moved to {destination}",
        "没有找到和 “{query}” 最接近的功能。": "Could not find a close match for “{query}”.",
        "服务商": "Provider",
        "模型名": "Model",
        "套用预设 URL": "Use Preset URL",
        "规范化 URL": "Normalize URL",
        "检查配置": "Check Config",
        "测试连接": "Test Connection",
        "请先填写 API Base。": "Please fill in API Base first.",
        "API Base 格式不正确，应以 http:// 或 https:// 开头。": "API Base is invalid. It should start with http:// or https://.",
        "API Base 看起来填到了接口路径；可点击“规范化 URL”自动修正。":
            "API Base looks like a full endpoint path. Click Normalize URL to fix it automatically.",
        "API Key 还没有填写。": "API Key has not been filled in yet.",
        "当前配置格式看起来正确，可以点击“测试连接”。":
            "The current configuration format looks valid. You can click Test Connection next.",
        "总览": "Overview",
        "这个入口页负责给第一次使用的人建立清晰路径。真正的核心操作已经单独放到“核心工作台”，不再堆在页面最底部。":
            "This page gives first-time users a clear path. The real work area now lives in its own Workspace page instead of being buried at the bottom.",
        "本地网页工作台": "Local Web Workspace",
        "适合自己用，也适合交给第一次接触项目的人。":
            "Built for your own workflow, and also friendly enough to hand to first-time users.",
        "推荐顺序是：先完成 setup 和 API 连接，再去核心工作台填写项目名、PDF 文件夹或 Zotero 配置，最后运行分析、大纲或全文写作。":
            "Recommended order: finish setup and API connection first, then go to Workspace to fill in the project name, PDF folder, or Zotero settings, and finally run analysis, outline, or full writing.",
        "这页先帮你把入口、模式和准备项理顺，再开始正式跑任务。":
            "This page helps you sort out the entry points, modes, and preparation steps before you launch a real run.",
        "进入核心工作台": "Open Workspace",
        "先做 setup": "Start with Setup",
        "当前输出目录": "Current Output Directory",
        "最近日志数量": "Recent Log Count",
        "预处理状态": "Preprocess Status",
        "已启用": "Enabled",
        "未启用": "Disabled",
        "选择输出目录": "Choose Output Directory",
        "选择 Zotero 报告文件": "Choose Zotero Report File",
        "选择 Zotero 库目录": "Choose Zotero Library Folder",
        "选择 PDF 文件夹": "Choose PDF Folder",
        "选择缓存目录": "Choose Cache Directory",
        "1. 环境与路径": "1. Environment & Paths",
        "在 GUI 内完成 setup、路径填写和输出目录配置。":
            "Complete setup, paths, and output directory settings inside the GUI.",
        "打开环境与路径": "Open Environment & Paths",
        "2. API 与模型": "2. APIs & Models",
        "阅读、写作、框架大纲和验证模型都可分别配置，并支持连通性测试。":
            "Reader, writer, outline, and validator models can be configured separately and tested directly.",
        "打开 API 页面": "Open API Page",
        "3. 核心工作台": "3. Workspace",
        "项目名、自由模式、分析、大纲和一键运行都集中在这里。":
            "Project name, free mode, analysis, outline, and run-all are all grouped here.",
        "开始运行": "Start",
        "三种使用方式": "Three Ways to Use It",
        "不用先记命令。先选路径，再选模式，再决定是先分析还是直接一键运行。":
            "You do not need to memorize commands first. Choose paths, choose a mode, then decide whether to analyze first or run everything directly.",
        "普通模式适合先批量读文献，再统一生成大纲和正文。":
            "Normal mode is best when you want to read papers in batch first, then generate the outline and full draft together.",
        "概念增强模式适合围绕某个核心概念补抓变量、定义与比较。":
            "Concept-enhanced mode is best when you want extra extraction around one core concept, including variables, definitions, and comparisons.",
        "自由模式适合先把你的研究意图聊清楚，再转成 prompt profile。":
            "Free mode is best when you want to clarify your research intent first and then turn it into a prompt profile.",
        "开始前快速检查": "Quick Check Before You Start",
        "先把这四件事看一遍，能减少很多中途报错和重复返工。":
            "A quick pass through these four checks can prevent a lot of mid-run errors and rework.",
        "先确认输出目录和 Zotero / PDF 路径可用。":
            "First confirm that the output directory and Zotero / PDF paths are valid.",
        "再检查阅读模型、写作模型和大纲模型都已经连通。":
            "Then make sure the reader, writer, and outline models all connect successfully.",
        "再检查阅读模型、写作模型、大纲模型和自由模式对话模型都已经连通。":
            "Then make sure the reader, writer, outline, and free-mode chat models all connect successfully.",
        "如果这批 PDF 质量参差不齐，建议先开预处理和 OCR 自动模式。":
            "If this batch of PDFs has mixed quality, enable preprocessing and OCR auto mode first.",
        "最后回到核心工作台，先跑分析，再决定是否一键运行。":
            "Finally, return to Workspace, run analysis first, and then decide whether to use Run All.",
        "把真正的工作区单独抽出来放在前面。你在这里输入项目、选择模式并直接运行，不需要翻很长的页面。":
            "The real work area now stands on its own. Enter project details, choose a mode, and run tasks here without scrolling through a long page.",
        "项目输入": "Project Input",
        "PDF 模式时填写 PDF 文件夹；Zotero 模式时可在“环境与路径”里填写 Zotero 报告路径。":
            "Use the PDF folder in PDF mode; for Zotero mode, set the Zotero report path in Environment & Paths.",
        "项目名": "Project Name",
        "PDF 文件夹": "PDF Folder",
        "概念增强模式概念词": "Concept for Enhanced Mode",
        "Zotero 报告路径": "Zotero Report Path",
        "打开 PDF 文件夹": "Open PDF Folder",
        "打开 Zotero 报告": "Open Zotero Report",
        "前往 Setup": "Go to Setup",
        "推荐操作顺序": "Suggested Order",
        "1. 先保存配置并测试 API。": "1. Save your configuration and test the APIs first.",
        "2. 再确定本次任务是普通模式、概念增强模式，还是自由模式。":
            "2. Decide whether this run is normal mode, concept-enhanced mode, or free mode.",
        "3. 仅分析文献适合先检查提取质量；一键运行适合稳定批量任务。":
            "3. Analyze-only is best for checking extraction quality; run-all is better for stable batch work.",
        "如果要做定制化综述，请在下方填写自由模式写作意图。":
            "If you want a customized review, describe your writing intent below in free mode.",
        "自由模式写作意图": "Free-Mode Writing Intent",
        "这里先写你的自然语言想法，系统会先把它整理成更适合后续分析与写作的 prompt profile。":
            "Write your idea in natural language here. The system will first turn it into a prompt profile for later analysis and writing.",
        "自由模式意图": "Free-Mode Intent",
        "例如：我想写消费者成熟度如何推导到测度，重点比较变量链路、理论视角和 research gap。":
            "Example: I want to explain how consumer maturity leads to measurement, focusing on variable chains, theoretical lenses, and research gaps.",
        "运行操作": "Run Actions",
        "核心按钮都放在这里，不再藏在页面最下面。":
            "The main action buttons all live here now instead of being buried at the bottom.",
        "仅分析文献": "Analyze Only",
        "生成大纲": "Generate Outline",
        "生成全文": "Generate Full Review",
        "一键运行": "Run All",
        "验证综述": "Validate Review",
        "重试失败论文": "Retry Failed Papers",
        "刷新日志": "Refresh Logs",
        "这里替代原来的命令行 setup。第一次使用的人只需要顺着页面填写，不需要进命令行敲配置。":
            "This replaces the old command-line setup. First-time users can now complete setup directly in the page without typing commands.",
        "基础路径": "Basic Paths",
        "如果你主要用 PDF 文件夹模式，可以只填输出目录；如果你用 Zotero 报告模式，再补 Zotero 相关路径。":
            "If you mainly use PDF folders, the output directory is enough. Add Zotero paths only if you use Zotero report mode.",
        "config.ini 路径": "config.ini Path",
        "输出目录": "Output Directory",
        "Zotero 库路径": "Zotero Library Path",
        "打开输出目录": "Open Output Directory",
        "首次使用建议": "First-Time Suggestions",
        "1. 先在这里填好输出目录和 Zotero 相关路径。":
            "1. Fill in the output directory and Zotero-related paths here first.",
        "2. 再去“API 与模型”页补模型、API Base 和 API Key。":
            "2. Then go to APIs & Models to fill in model names, API Base, and API Keys.",
        "3. 如果 API Base 填错格式，保存时会自动规范化。":
            "3. If the API Base format is wrong, it will be normalized automatically when you save.",
        "4. 配置保存后，API Key 会写入 `.env`，不用手改文本文件。":
            "4. After saving, API Keys will be written into `.env`, so you do not need to edit text files manually.",
        "前往 API 与模型": "Go to APIs & Models",
        "前往性能与预处理": "Go to Performance & Preprocessing",
        "阅读模型、写作模型、独立大纲模型和验证模型都在这里分开配置。每块都支持 URL 预设、自动规范化和连通性测试。":
            "Reader, writer, standalone outline, and validator models are configured separately here. Each card supports URL presets, normalization, and connectivity testing.",
        "阅读模型、写作模型、大纲模型、自由模式对话模型和验证模型都在这里分开配置。每块都支持 URL 预设、自动规范化和连通性测试。":
            "Reader, writer, outline, free-mode chat, and validator models are configured separately here. Each card supports URL presets, normalization, and connectivity testing.",
        "阅读模型": "Reader Model",
        "优先负责文献分析与阶段一抽取。": "Primarily used for literature analysis and stage-one extraction.",
        "备用阅读模型": "Backup Reader Model",
        "当主阅读模型失败或限流时，系统可以兜底。":
            "Used as a fallback when the main reader model fails or is rate-limited.",
        "写作模型": "Writer Model",
        "负责大段综述写作与章节生成。": "Used for long-form review writing and section generation.",
        "大纲 / 自由模式模型": "Outline / Free-Mode Model",
        "优先负责框架大纲与自由模式 prompt 规划；未配置时可回退到写作模型。":
            "Used first for outline planning and free-mode prompt planning; falls back to the writer model if left blank.",
        "验证模型": "Validator Model",
        "用于综述校验和质量复查。": "Used for review validation and quality checks.",
        "这一页专门控制并发、验证、PDF 预处理、OCR 和本地 RAG。这样 setup 页面不会显得过于拥挤。":
            "This page is dedicated to concurrency, validation, PDF preprocessing, OCR, and local RAG, so setup stays cleaner.",
        "运行参数": "Runtime Settings",
        "最大并发": "Max Workers",
        "API 重试次数": "API Retry Attempts",
        "启用阶段一验证": "Enable Stage 1 Validation",
        "启用阶段二验证": "Enable Stage 2 Validation",
        "PDF 预处理": "PDF Preprocessing",
        "默认会先做缓存和诊断，再交给 AI 分析，减少直接啃 PDF 时的不稳定。":
            "The system now caches and diagnoses PDFs before AI analysis to reduce instability caused by raw PDF parsing.",
        "启用预处理": "Enable Preprocessing",
        "强制重建缓存": "Force Cache Rebuild",
        "缓存目录": "Cache Directory",
        "OCR 语言": "OCR Languages",
        "提取策略": "Extractor Profile",
        "OCR 模式": "OCR Mode",
        "启用本地 RAG": "Enable Local RAG",
        "RAG 后端": "RAG Backend",
        "OCR 默认只在疑似扫描页、无文本页或提取质量过低时触发，不会一上来就全量 OCR。":
            "OCR is triggered only for scanned-looking pages, pages without text, or pages with low extraction quality; it is not applied to every page by default.",
        "这里集中放状态、日志和目录入口。运行任务后你可以直接在这里看最近进展，而不需要回命令行。":
            "Status, logs, and folder shortcuts are grouped here so you can check progress without returning to the terminal.",
        "当前状态": "Current Status",
        "打开日志目录": "Open Log Directory",
        "最近日志文件": "Latest Log File",
        "这页是面向第一次使用者的说明，尽量把理解成本降下来。后面如果你愿意，我还可以继续做成更完整的新手向导。":
            "This page is written for first-time users and aims to reduce the learning curve. It can be expanded into a fuller onboarding guide later.",
        "普通模式": "Normal Mode",
        "适合常规综述。先分析文献，再生成大纲和正文。":
            "Best for standard review writing. Analyze papers first, then generate an outline and the full text.",
        "概念增强模式": "Concept-Enhanced Mode",
        "适合围绕某个概念做更聚焦的抽取与比较。":
            "Best for focused extraction and comparison around a specific concept.",
        "自由模式": "Free Mode",
        "适合先说出你的研究想法，让系统先整理成更好的 prompt profile。":
            "Best when you want to describe your research idea first and let the system turn it into a stronger prompt profile.",
        "关于 OCR 和预处理": "About OCR and Preprocessing",
        "默认不是全量 OCR。系统会先判断 PDF 是否有可用文本，再只对异常页触发 OCR。这样更省性能，也更适合普通电脑。":
            "OCR is not applied to every page by default. The system first checks whether a PDF already contains usable text, then applies OCR only to abnormal pages. This is lighter on performance and better for ordinary laptops.",
        "如果后续你想继续增强前端体验，最自然的下一步会是“Python 后端 + JavaScript 前端”。当前这个 NiceGUI 版本则更适合快速把本地工具做成可用的网页工作台。":
            "If you later want a richer frontend experience, the natural next step is a Python backend plus a JavaScript frontend. The current NiceGUI version is better for quickly turning a local research tool into a usable web workspace.",
    },
}

ACTION_LABELS["zh-CN"].update(
    {
        "generate_section": "补写指定章节",
        "retry_review_failed": "重试失败章节",
    }
)

ACTION_LABELS["en"].update(
    {
        "generate_section": "generate section",
        "retry_review_failed": "retry failed sections",
    }
)

TRANSLATIONS["en"].update(
    {
        "章节操作": "Section Actions",
        "章节号": "Section Number",
        "补写指定章节": "Generate Selected Section",
        "重试失败章节": "Retry Failed Sections",
        "阶段二重试": "Stage 2 Retry",
        "启用阶段二自动重试": "Enable Stage 2 Auto Retry",
        "阶段二最大重试轮数": "Stage 2 Max Retry Rounds",
        "阶段二基础等待秒数": "Stage 2 Base Delay (s)",
        "阶段二最大等待秒数": "Stage 2 Max Delay (s)",
        "高级 / 实验功能": "Advanced / Experimental",
        "验证功能默认关闭，暂时作为实验功能保留。": "Validation is off by default and currently kept as an experimental feature.",
        "任务进度": "Task Progress",
        "当前任务": "Current Task",
        "当前阶段": "Current Stage",
        "总体进度": "Overall Progress",
        "阶段进度": "Stage Progress",
        "当前对象": "Current Item",
        "成功 / 失败 / 剩余": "Success / Failed / Remaining",
        "重试轮次": "Retry Round",
        "已耗时": "Elapsed",
        "暂无运行中的任务": "No active task right now.",
        "等待模型返回或执行长任务中…": "Waiting for the model or another long-running step to finish...",
        "请先输入有效的章节号。": "Please enter a valid section number first.",
        "正在刷新任务进度…": "Refreshing task progress...",
        "阶段二自动重试会在全文生成时自动补跑失败章节。": "Stage 2 auto retry will retry failed sections after the initial full-review pass.",
        "如果某一章中途失败，可以单独补写，或者只补跑失败章节。": "If a chapter fails midway, you can regenerate just that section or retry only the failed sections.",
        "配置路径": "Configure Path",
        "手动输入或用选择按钮更新路径。": "Enter a path manually or use the browse button to update it.",
        "浏览并选择": "Browse",
        "取消": "Cancel",
        "保存路径设置": "Save Path",
    }
)

TRANSLATIONS["en"].update(
    {
        "先确定这次任务从 PDF 文件夹还是 Zotero 报告开始，再补充项目名。":
            "Start by deciding whether this run uses a PDF folder or a Zotero report, then fill in the project name.",
        "如果这次要围绕某个核心概念补抓变量、定义和比较关系，就填写概念词。普通模式可以留空。":
            "Fill in the concept only when this run needs extra extraction around one core idea, including variables, definitions, or comparisons. In normal mode, you can leave it blank.",
        "模式补充": "Mode-Specific Inputs",
        "普通模式可以留空；概念增强和自由模式只填写和这次任务直接相关的补充信息。":
            "You can leave this blank in normal mode. For concept-enhanced and free mode, only add the extra context that matters for this run.",
        "如果这次要围绕某个核心概念补抓变量、定义和比较关系，就填写概念词。":
            "Fill in the concept only when this run needs extra extraction around one core idea, including variables, definitions, or comparisons.",
        "如果你希望系统先整理研究意图，再生成更贴合目标的 prompt profile，就把想法写在这里。":
            "Write here when you want the system to organize your research intent first and then build a prompt profile that better matches your goal.",
        "主流程操作": "Primary Workflow Actions",
        "把主流程按钮集中放在一起，只保留真正代表分析链路的四个入口。":
            "The main workflow buttons are grouped here so only the four core entry points stay in the primary action area.",
        "先检查文献提取、预处理和结构化结果是否稳定。":
            "Use this first to verify that extraction, preprocessing, and structured outputs look stable.",
        "在分析结果基础上先搭出综述结构。":
            "Generate the review structure first, using the analysis results as a base.",
        "直接生成正文，适合已经确认过结构和素材的任务。":
            "Generate the full draft directly when the structure and materials are already confirmed.",
        "从分析到正文一口气跑完，适合稳定的批量流程。":
            "Run the whole chain from analysis to full draft in one go when your batch workflow is already stable.",
        "补跑与质检": "Recovery & Validation",
        "把修复、补跑和验证入口单独放在这里，避免和首次运行按钮混在一起。":
            "Recovery, retry, and validation actions live here so they do not get mixed into the first-run controls.",
        "失败论文": "Failed Papers",
        "如果只有个别论文失败，可以单独补跑，不影响已经完成的结果。":
            "If only a few papers failed, retry them here without disturbing the results that already completed.",
        "质量检查": "Quality Check",
        "把准备项单独放到侧边后，这里只保留真正会影响流程成败的检查。":
            "Now that preparation items live in the sidebar, this card keeps only the checks that directly affect whether the run succeeds.",
        "最后决定是先跑分析，还是直接一键运行。":
            "Finally, decide whether to analyze first or go straight to Run All.",
        "工作台导航": "Workspace Navigation",
        "配置、日志和目录入口单独收纳在这里，不再和运行按钮放在同一组。":
            "Configuration, logs, and folder shortcuts are grouped here instead of being mixed with the run buttons.",
        "查看日志与产物": "View Logs & Outputs",
        "自由模式对话规划器": "Free-Mode Chat Planner",
        "先和规划助手多轮聊清楚你的综述想法，再把当前规划应用到本次任务。":
            "Clarify your review idea with the planner across multiple turns, then apply the current plan to this task.",
        "对话记录": "Conversation Log",
        "当前 profile 草案": "Current Profile Draft",
        "继续告诉规划助手": "Continue the Conversation",
        "例如：文件夹里主要有概念 A 和 B，我想写 A 如何推导到 B，重点比较理论解释、变量链路和 research gap。":
            "Example: The folder mainly contains concepts A and B. I want to explain how A leads to B, focusing on theoretical explanations, variable chains, and research gaps.",
        "发送给规划助手": "Send to Planner",
        "应用到本次任务": "Apply to This Task",
        "清空自由模式对话": "Clear Free-Mode Chat",
        "自由模式会先和你多轮澄清写作意图，再把对话整理成可执行的 prompt profile。":
            "Free mode will clarify your writing intent across multiple turns and then turn the conversation into an executable prompt profile.",
        "对话过程中提炼出的研究目标、概念关系、关注重点和优化 prompt 会显示在这里。":
            "The research goal, concept relationship, focus points, and optimized prompt extracted from the conversation will appear here.",
        "你": "You",
        "规划助手": "Planner",
        "研究目标": "Research Goal",
        "概念关系 / 主线": "Concept Relationship / Main Thread",
        "关注重点": "Focus Points",
        "排除项": "Exclusions",
        "理论 / 变量焦点": "Theory / Variable Focus",
        "结构偏好": "Outline Preferences",
        "生成后的优化 prompt": "Generated Optimized Prompt",
        "对话摘要": "Conversation Notes",
        "自由模式正在整理你的想法…": "Free mode is organizing your idea...",
        "自由模式已应用到本次任务：{target}": "Free mode has been applied to this task: {target}",
        "当前规划已经比较完整，可以直接应用到本次任务。":
            "The current plan is already clear enough to be applied to this task.",
        "当前规划还在澄清阶段，你可以继续补充，也可以先应用草案。":
            "The current plan is still being clarified. You can continue adding details or apply the draft now.",
        "先告诉规划助手你想写什么，它会边聊边帮你收束成适合综述流程的 prompt。":
            "Start by telling the planner what you want to write. It will narrow the idea into a prompt that fits the review workflow.",
        "本轮对话返回后，这里会更新仍需补充的信息。":
            "Once this turn returns, the remaining information to clarify will be updated here.",
        "后续运行会优先使用这份已应用的自由模式 profile。":
            "Subsequent runs will prefer this applied free-mode profile.",
        "请先输入你想和自由模式讨论的内容。": "Please enter what you want to discuss with free mode first.",
        "当前有任务正在占用工作台，请稍后再继续自由模式对话。":
            "The workspace is currently busy with another task. Please continue the free-mode chat afterward.",
        "自由模式本轮规划失败，请检查 Free_Mode_API / Outline_API 配置后重试。":
            "This free-mode planning turn failed. Please check the Free_Mode_API / Outline_API configuration and try again.",
        "自由模式规划已更新，你可以继续追问，或把当前规划应用到本次任务。":
            "The free-mode plan has been updated. You can keep refining it or apply the current plan to this task.",
        "自由模式对话已清空。": "The free-mode conversation has been cleared.",
        "当前有任务正在占用工作台，请稍后再应用自由模式规划。":
            "The workspace is currently busy with another task. Please apply the free-mode plan afterward.",
        "请先和自由模式对话，再决定是否应用到本次任务。":
            "Talk with free mode first, then decide whether to apply the plan to this task.",
        "请先填写项目名，再应用自由模式规划。":
            "Please fill in the project name before applying the free-mode plan.",
        "自由模式 profile 应用失败，请检查 Free_Mode_API / Outline_API 配置后重试。":
            "Applying the free-mode profile failed. Please check the Free_Mode_API / Outline_API configuration and try again.",
        "自由模式对话还没有应用到本次任务。请先应用当前规划，或清空对话后再运行。":
            "The free-mode conversation has not been applied to this task yet. Please apply the current plan or clear the conversation before running.",
        "大纲模型": "Outline Model",
        "优先负责框架大纲规划；未配置时可回退到写作模型。":
            "Primarily used for outline planning; falls back to the writer model if left blank.",
        "自由模式对话模型": "Free-Mode Chat Model",
        "优先负责自由模式前置对话规划；未配置时可回退到大纲模型。":
            "Primarily used for free-mode chat planning; falls back to the outline model if left blank.",
    }
)


def translate(language: str, key: str) -> str:
    if language == "zh-CN":
        return key
    return TRANSLATIONS.get(language, {}).get(key, key)


def action_label(language: str, action: str) -> str:
    return ACTION_LABELS.get(language, ACTION_LABELS["zh-CN"]).get(action, action)
