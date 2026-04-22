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
    "zh-CN": {},
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

TRANSLATIONS["zh-CN"].update(
    {
        "已清空已完成任务 {count}": "已清空 {count} 个已完成任务",
        "清空失败: {e}": "清空失败: {e}",
    }
)

TRANSLATIONS["en"].update(
    {
        "任务": "Tasks",
        "队列管理": "Queue Management",
        "管理后台任务队列": "Manage background task queue",
        "管理后台任务队列，支持排队、重排、取消、重试以及队列文件导入导出。":
            "Manage background jobs, including queueing, reordering, cancelling, retrying, and queue-file import/export.",
        "队列状态": "Queue Status",
        "待处理": "Pending",
        "运行中": "Running",
        "已完成": "Completed",
        "失败": "Failed",
        "已取消": "Cancelled",
        "任务类型": "Job Type",
        "项目名": "Project Name",
        "创建时间": "Created At",
        "开始时间": "Started At",
        "完成时间": "Completed At",
        "错误信息": "Error Message",
        "结果摘要": "Result Summary",
        "重试次数": "Retry Count",
        "刷新队列": "Refresh Queue",
        "取消任务": "Cancel Job",
        "重试任务": "Retry Job",
        "清空已完成": "Clear Completed",
        "暂无队列任务": "No queued jobs yet.",
        "队列服务未初始化": "Queue service not initialized.",
        "不能删除运行中的任务": "Running jobs cannot be deleted.",
        "任务已删除: {job_id}": "Deleted job: {job_id}",
        "删除任务失败: {e}": "Failed to delete job: {e}",
        "任务已重置并将重试: {job_id}": "Job reset and ready to retry: {job_id}",
        "只能重试失败或已取消的任务": "Only failed or cancelled jobs can be retried.",
        "重试任务失败: {e}": "Failed to retry job: {e}",
        "队列已保存到: {file_path}": "Queue saved to: {file_path}",
        "保存队列失败: {e}": "Failed to save queue: {e}",
        "队列已从: {file_path} 加载": "Queue loaded from: {file_path}",
        "加载队列失败: {e}": "Failed to load queue: {e}",
        "已清空已完成任务 {count}": "Cleared {count} completed jobs",
        "清空失败: {e}": "Clear failed: {e}",
        "添加任务到队列": "Add Jobs to Queue",
        "队列文件操作": "Queue File Operations",
        "队列任务列表": "Queued Jobs",
        "队列已刷新": "Queue refreshed.",
        "队列按当前列表顺序执行；可用每行的“上移 / 下移”按钮调整顺序。":
            "Jobs run in the order shown here. Use each row's Move Up / Move Down buttons to adjust the queue.",
        "可先批量添加多个任务到草稿，再统一提交到队列。支持 PDF 文件夹和 Zotero 报告混合排队。":
            "Draft multiple jobs first, then submit them to the queue together. PDF-folder and Zotero-report jobs can be mixed.",
        "队列页默认提交标准任务；如果要先做概念增强或自由模式规划，建议先在工作台确认后再入队。":
            "The queue page submits standard tasks by default. If you need concept-enhanced or free-mode planning first, confirm it in the workspace before queueing it.",
        "请输入项目名": "Enter a project name",
        "请输入队列文件路径": "Enter a queue file path",
        "请输入 PDF 文件夹路径": "Enter a PDF folder path",
        "请输入 Zotero 报告路径": "Enter a Zotero report path",
        "加入草稿": "Add to Draft",
        "立即入队": "Queue Now",
        "提交草稿": "Commit Draft",
        "清空草稿": "Clear Draft",
        "移除": "Remove",
        "上移": "Move Up",
        "下移": "Move Down",
        "重试": "Retry",
        "删除": "Delete",
        "任务顺序已更新": "Queue order updated",
        "调整任务顺序失败: {e}": "Failed to adjust queue order: {e}",
        "保存队列": "Save Queue",
        "加载队列": "Load Queue",
        "队列文件路径": "Queue File Path",
        "队列草稿为空。你可以先添加多个任务，再统一提交到队列。":
            "The queue draft is empty. Add multiple jobs first, then commit them to the queue together.",
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
        "高级 / 可选功能": "Advanced / Optional Features",
        "验证功能默认关闭，暂时作为实验功能保留。": "Validation is off by default and currently kept as an experimental feature.",
        "这里只保留仍然建议用户直接控制的高级项。综述验证是可选增强步骤，默认不改变主流程。":
            "Only the advanced settings that still make sense for direct user control are kept here. Review validation is optional and does not change the default workflow.",
        "综述验证是可选增强步骤。需要额外核查时再运行，不影响默认主流程。":
            "Review validation is an optional enhancement. Run it when you need an extra check; it does not change the default workflow.",
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
        "阶段一复用开启后，会自动扫描历史输出并尽量跳过已经分析过的论文。":
            "When stage-1 reuse is enabled, the app scans historical outputs automatically and tries to skip papers that have already been analyzed.",
        "大多数真实产物现在都写入 output/<project_name>__<job_id>/ 工作区；旧的 output/<project_name>/ 更像兼容指针目录。":
            "Most real outputs now live in output/<project_name>__<job_id>/ workspaces; the older output/<project_name>/ path is now closer to a compatibility pointer directory.",
        "配置路径": "Configure Path",
        "手动输入或用选择按钮更新路径。": "Enter a path manually or use the browse button to update it.",
        "浏览并选择": "Browse",
        "取消": "Cancel",
        "保存路径设置": "Save Path",
    }
)

TRANSLATIONS["en"].update(
    {
        "开始使用": "Getting Started",
        "运行任务": "Run Tasks",
        "高级功能": "Advanced",
        "工作台": "Workspace",
        "队列": "Queue",
        "结果与日志": "Results & Logs",
        "项目入口与第一轮路径": "Project entry and first-run path",
        "第一次使用先看这里": "Start here if it is your first time",
        "选择输入来源、运行方式与主流程": "Choose the input source, run mode, and primary flow",
        "查看最新工作区、主要产物与日志": "View the latest workspace, primary artifacts, and logs",
        "基础路径与输出目录": "Basic paths and output directory",
        "阅读、写作、大纲、自由模式与验证模型": "Reader, writer, outline, free-mode, and validator models",
        "并发、OCR、缓存、RAG 与可选验证": "Concurrency, OCR, cache, RAG, and optional validation",
        "批量任务、后台恢复与重排": "Batch jobs, background recovery, and reordering",
        "工作台已就绪。先完成设置，再按“输入来源 → 运行方式 → 主流程”开始第一轮。":
            "Workspace is ready. Finish setup first, then start your first run with Input Source → Run Mode → Primary Flow.",
        "任务起点": "Task Starting Point",
        "先选输入来源，再填写项目名。第一轮建议优先跑“仅分析文献”，确认摘要质量后再继续。":
            "Choose the input source first, then fill in the project name. For the first run, start with Analyze Only and continue after confirming summary quality.",
        "输入来源": "Input Source",
        "PDF 文件夹模式": "PDF Folder Mode",
        "Zotero 报告模式": "Zotero Report Mode",
        "PDF 模式适合直接扫描文件夹；Zotero 模式适合从 report + library 继续。":
            "PDF mode is best for scanning a folder directly; Zotero mode is best when continuing from a report plus library.",
        "Zotero 模式需要 report 和 library 两个路径；如果没配好，先去“环境与路径”页面补齐。":
            "Zotero mode needs both the report path and the library path. If either is missing, fill them in on the Environment & Paths page first.",
        "打开 Zotero 库路径": "Open Zotero Library Folder",
        "运行方式": "Run Mode",
        "先用普通模式跑通第一轮；只有在确实需要额外概念抽取或先聊清写作意图时，再切换模式。":
            "Use normal mode for the first successful run. Switch only when you truly need concept-focused extraction or a planning conversation first.",
        "普通模式：适合第一次运行和大多数常规任务。":
            "Normal mode is best for a first run and for most routine tasks.",
        "概念增强：只在你要围绕某个核心概念补抓变量、定义和比较时使用。":
            "Use concept-enhanced mode only when you need extra variables, definitions, and comparisons around one core concept.",
        "自由模式：先和规划助手聊清目标，再把规划应用到本次任务。":
            "Free mode lets you clarify the goal with the planner first and then apply the plan to the task.",
        "概念增强（仅在概念模式下填写）": "Concept Enhancement (Only in Concept Mode)",
        "自由模式规划（仅在自由模式下展开）": "Free-Mode Planning (Shown Only in Free Mode)",
        "高级：复用已有摘要（一般可跳过）": "Advanced: Reuse Existing Summaries (Usually Optional)",
        "当你要从已有 summaries.json 继续生成大纲/正文，或想给阶段一提供额外复用池时，再展开这里。":
            "Expand this only when you want to continue from existing summaries.json files or provide extra reuse pools for stage 1.",
        "第一次运行建议": "First-Run Recommendation",
        "第一次使用建议": "First-Run Recommendation",
        "如果你是第一次使用，就按这条主路径走，不需要一次把所有高级能力都打开。":
            "If this is your first time, follow this main path and do not open every advanced feature at once.",
        "先在“设置”里确认输出目录、输入路径和 API 模型都已连通。":
            "First, confirm that the output directory, input paths, and API models are all ready in Settings.",
        "再到这里选择输入来源：PDF 文件夹或 Zotero。":
            "Then come back here and choose the input source: PDF folder or Zotero.",
        "首次运行优先点击“仅分析文献”，确认摘要、预处理和提取质量。":
            "For the first run, click Analyze Only first to confirm summary quality, preprocessing, and extraction stability.",
        "确认第一轮结果没问题后，再继续生成大纲、全文，或最后再使用一键运行。":
            "Once the first-round results look good, continue with outline generation, full review generation, or Run All at the end.",
        "相关入口": "Related Pages",
        "这里只保留工作台最常用的相关入口，减少工具按钮到处重复出现。":
            "Only the most relevant links for the workspace stay here, so utility buttons do not repeat everywhere.",
        "前往设置": "Open Settings",
        "查看结果与日志": "Open Results & Logs",
        "当前选择的是 PDF 文件夹模式，请先填写 PDF 文件夹。":
            "PDF folder mode is selected. Please fill in the PDF folder first.",
        "当前选择的是 Zotero 模式，请先填写 Zotero 报告路径。":
            "Zotero mode is selected. Please fill in the Zotero report path first.",
        "Zotero 模式还需要填写 Zotero 库路径。":
            "Zotero mode also requires the Zotero library path.",
        "当前已有任务正在运行。": "A task is already running.",
        "只能取消运行中的任务": "Only running jobs can be cancelled.",
        "测试模式：已模拟执行 {action_label}。": "Test mode: simulated {action_label}.",
        "任务入队失败，请检查当前输入后重试。": "Failed to enqueue the task. Please check the current inputs and try again.",
        "队列服务未初始化，无法启动任务。": "Queue service is not initialized, so the task cannot start.",
        "请查看日志与最近产物了解失败原因。": "Check the logs and recent artifacts for the failure reason.",
        "总览": "Overview",
        "这里先帮你建立一条清楚、安静的第一轮路径：先设置，再选输入来源和运行方式，最后进入工作台执行。":
            "This page helps you build a calm and clear first-run path: setup first, then choose the input source and run mode, and finally open the workspace.",
        "先跑通第一轮，再逐步打开高级能力。":
            "Get the first run working before opening advanced features.",
        "这个项目最适合用“研究工作台”的方式理解：先准备路径和模型，再进入工作台按输入来源、运行方式和主流程顺序推进。":
            "This project works best when treated like a research workspace: prepare paths and models first, then move through input source, run mode, and the primary flow.",
        "如果你是第一次使用，不需要一开始就接触队列、自由模式或恢复操作。先把第一轮摘要跑稳最重要。":
            "If this is your first time, you do not need queueing, free mode, or recovery right away. Getting the first batch of summaries stable matters most.",
        "进入工作台": "Enter Workspace",
        "第一次使用，只要走这五步": "For the First Run, Just Follow These Five Steps",
        "把第一次跑通当成目标，而不是一开始就把所有功能都学完。":
            "Treat the first successful run as the goal, instead of trying to learn every feature immediately.",
        "先完成基础设置": "Finish Basic Setup",
        "先在设置页填好输出目录、输入路径和必要的模型配置。":
            "Fill in the output directory, input paths, and required model settings on the Settings pages first.",
        "再检查 API 连通": "Check API Connectivity",
        "阅读、写作和大纲模型至少要保证能连通一次。":
            "At minimum, the reader, writer, and outline models should each connect successfully once.",
        "进入工作台选择输入来源": "Choose the Input Source in the Workspace",
        "先确定这次是 PDF 文件夹还是 Zotero 模式。":
            "Decide whether this run starts from a PDF folder or from Zotero.",
        "第一轮先跑仅分析文献": "Start the First Run with Analyze Only",
        "先确认结构化摘要、预处理和抽取质量，再决定是否继续大纲和全文。":
            "Confirm the structured summaries, preprocessing, and extraction quality before continuing to outline or full review.",
        "最后再看结果与日志": "Then Review Results & Logs",
        "真正的输出通常在 job workspace 里；日志只是辅助。":
            "The real outputs usually live in the job workspace; logs are supporting evidence.",
        "两种输入来源": "Two Input Sources",
        "先决定这次从哪里开始，比记住命令更重要。":
            "Deciding where this run starts is more important than memorizing commands.",
        "适合你已经把文献 PDF 放在一个文件夹里，想直接开始批量分析。":
            "Best when your paper PDFs are already in one folder and you want to start batch analysis directly.",
        "适合你已经有 Zotero report 和 library，希望沿着已有文献整理结果继续。":
            "Best when you already have a Zotero report and library and want to continue from your existing organization.",
        "三种运行方式": "Three Run Strategies",
        "不是三选一的界面风格，而是三种工作策略。":
            "These are not three page styles to choose from, but three ways to work.",
        "最适合第一次运行：先分析文献，再决定是否继续大纲和全文。":
            "Best for a first run: analyze the papers first, then decide whether to continue to outline and full text.",
        "只在你要围绕某个核心概念补抓变量、定义与比较时使用。":
            "Use this only when you need extra variables, definitions, and comparisons around one core concept.",
        "适合先和规划助手聊清目标，再把当前规划应用到本次任务。":
            "Best when you want to clarify the goal with the planner first and then apply the current plan to this task.",
        "适合先和规划助手聊清楚目标，再把当前规划应用到本次任务。":
            "Best when you want to talk through the goal with the planner first and then apply the current plan to this task.",
        "工作台": "Workspace",
        "这里按“输入来源 → 运行方式 → 主流程”的顺序组织。高级复用、补跑和验证都放在次级区域，避免第一次使用被打断。":
            "This page is organized as Input Source → Run Mode → Primary Flow. Advanced reuse, recovery, and validation are secondary so they do not interrupt the first run.",
        "第一次使用建议按这个顺序：仅分析文献 → 生成大纲 → 生成全文。只有在流程稳定后，再使用一键运行。":
            "For a first run, use this order: Analyze Only → Generate Outline → Generate Full Review. Use Run All only after the workflow becomes stable.",
        "如果你是第一次跑这个项目，先点“仅分析文献”。如果已有可靠摘要或历史工作区，再继续点大纲、全文或验证。":
            "If this is the first time you run this project, click Analyze Only first. Move on to outline, full review, or validation only when you already have reliable summaries or a previous workspace.",
        "补跑、恢复与验证（按需展开）": "Recovery, Retry, and Validation (Expand When Needed)",
        "结果与日志": "Results & Logs",
        "优先查看最近一次任务的工作区和主要产物；日志是辅助线索，不再是唯一入口。":
            "Start with the latest workspace and primary artifacts; logs are supporting clues instead of the only entry point.",
        "最近一次任务": "Latest Task",
        "当前还没有可识别的任务工作区。先去工作台运行一次任务。":
            "No recognizable task workspace has been found yet. Run a task from the workspace first.",
        "项目": "Project",
        "任务 ID": "Job ID",
        "工作区状态": "Workspace Status",
        "更新时间": "Updated At",
        "工作区路径": "Workspace Path",
        "打开工作区": "Open Workspace",
        "打开产物目录": "Open Artifacts",
        "打开报告目录": "Open Reports",
        "打开注册表": "Open Registry",
        "注册表": "Registry",
        "主要产物": "Primary Artifacts",
        "结构化摘要": "Structured Summaries",
        "综述大纲": "Review Outline",
        "分析表": "Analysis Spreadsheet",
        "综述文档": "Review Document",
        "失败报告": "Failure Report",
        "验证报告": "Validation Report",
        "目前还没有检出的主要产物。": "No primary artifacts have been detected yet.",
        "队列": "Queue",
        "适合稳定后批量跑、后台恢复和长任务管理；不建议作为第一次使用的主入口。":
            "Best for stable batch runs, background recovery, and long-job management. It is not the recommended first entry point for new users.",
        "队列摘要": "Queue Summary",
        "当前队列顺序会直接影响后台执行顺序。批量任务稳定后再来这里会更轻松。":
            "The current queue order directly affects background execution order. This page becomes much easier once your batch workflow is stable.",
        "开始运行队列任务...": "Starting queued jobs...",
        "队列运行完成！": "Queue run completed!",
        "使用引导": "Guide",
        "这页按第一次使用的顺序讲清楚：输入、设置、运行、结果和恢复。尽量让你不需要先读命令行帮助也能跑通。":
            "This page explains the first-run order: input, setup, execution, results, and recovery. The goal is to let you succeed without reading the CLI help first.",
        "第一次运行，只看这一页也能开始": "You Can Start from This Page Alone",
        "下面这五步对应 GUI 里最重要的页面和动作。先跑通，再回头用高级功能。":
            "These five steps map to the most important pages and actions in the GUI. Get the first run working, then come back for advanced features.",
        "准备输入材料": "Prepare Your Input Materials",
        "PDF 模式只需要文件夹；Zotero 模式需要 report 和 library。":
            "PDF mode only needs a folder; Zotero mode needs both a report and a library.",
        "完成设置与模型连接": "Finish Setup and Model Connectivity",
        "先去设置页填路径，再检查 Reader / Writer / Outline 等模型是否可用。":
            "Fill in the paths on the Settings pages first, then check whether the Reader / Writer / Outline models are available.",
        "进入工作台选择运行方式": "Choose a Run Mode in the Workspace",
        "普通模式最适合第一次跑；概念增强和自由模式只在有明确需要时再用。":
            "Normal mode is best for a first run; use concept-enhanced and free mode only when there is a clear need.",
        "去结果与日志页看工作区": "Open Results & Logs to Inspect the Workspace",
        "最新 job workspace 和主要产物比原始日志更值得先看。":
            "The latest job workspace and primary artifacts are more important to check first than raw logs.",
        "输入方式说明": "Input Modes",
        "运行方式说明": "Run Modes",
        "适合你已经准备好 PDF 文件夹，想直接开始批量分析。":
            "Best when your PDF folder is ready and you want to begin batch analysis immediately.",
        "适合你已经整理好 Zotero report 和文献库，希望沿着现有整理结果继续。":
            "Best when your Zotero report and library are already organized and you want to continue from that structure.",
        "最稳妥，最适合第一轮。": "The safest option and the best choice for a first run.",
        "适合围绕某个概念做更聚焦的抽取、定义和比较。":
            "Best for more focused extraction, definition work, and comparison around one concept.",
        "适合先把研究意图聊清楚，再把当前规划应用到本次任务。":
            "Best when you want to clarify the research intent first and then apply the current plan to this task.",
        "例如：我想围绕概念 A 如何推导到概念 B 来写综述，重点比较变量链路、理论解释和 research gap。":
            "Example: I want to write a review about how concept A leads to concept B, focusing on variable chains, theoretical explanation, and the research gap.",
        "关于 OCR、复用和工作区": "About OCR, Reuse, and Workspaces",
        "启用综述验证": "Enable Review Validation",
        "请先填写模型名。": "Please fill in the model name first.",
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
        "综述验证是可选增强步骤。需要额外核查时再运行，不影响默认主流程。":
            "Review validation is an optional enhancement. Run it when you need an extra check; it does not change the default workflow.",
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
        "使用说明": "How to Use It",
        "队列页现在覆盖了常见的排队与恢复动作。你可以：":
            "The queue page now covers the common queueing and recovery actions. You can:",
        "1. 点击'运行队列'按钮执行所有待处理任务":
            "1. Click Run Queue to execute all pending jobs.",
        "2. 在任务列表里用'上移'和'下移'按钮调整执行顺序":
            "2. Use Move Up and Move Down in the job list to adjust execution order.",
        "3. 对失败或已取消的任务点击'重试'按钮":
            "3. Click Retry for failed or cancelled jobs.",
        "4. 对运行中的任务点击'取消'按钮":
            "4. Click Cancel for running jobs.",
        "5. 点击'清空已完成'按钮清理已完成的任务":
            "5. Click Clear Completed to remove finished jobs.",
        "6. 在'添加任务到队列'区域添加新任务":
            "6. Add new jobs in the Add Jobs to Queue section.",
        "7. 对非运行中的任务点击'删除'按钮移除任务":
            "7. Click Delete on non-running jobs to remove them.",
        "8. 使用'保存队列'和'加载队列'功能管理队列文件":
            "8. Use Save Queue and Load Queue to manage queue files.",
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
        "阶段一复用开启后，会自动扫描历史输出并尽量跳过已经分析过的论文。":
            "When stage-1 reuse is enabled, the app scans historical outputs automatically and tries to skip papers that have already been analyzed.",
        "大多数真实产物现在都写入 output/<project_name>__<job_id>/ 工作区；旧的 output/<project_name>/ 更像兼容指针目录。":
            "Most real outputs now live in output/<project_name>__<job_id>/ workspaces; the older output/<project_name>/ path is now closer to a compatibility pointer directory.",
    }
)


TRANSLATIONS["en"].update(
    {
        "总览页只保留方向和状态，不再重复输入来源和运行方式的长说明。":
            "The Overview page now keeps only direction and status, instead of repeating the long explanations for input sources and run modes.",
        "现在建议做什么": "What to Do Next",
        "工作台里只负责真正运行，不再把说明文字铺满整个页面。":
            "The Workspace now focuses on actually running tasks instead of filling the page with repeated explanations.",
        "如果你需要完整的新手解释，再去“使用引导”页查看输入方式、运行方式和工作区说明。":
            "If you want the full beginner explanation, open the Guide page for input modes, run modes, and workspace notes.",
        "当前工作台快照": "Current Workspace Snapshot",
        "这里看当前配置状态；更完整的解释已经集中到“使用引导”页。":
            "Use this section for the current configuration snapshot; the fuller explanation now lives on the Guide page.",
        "解析策略": "Parser Strategy",
        "MinerU": "MinerU",
        "常用入口": "Common Pages",
        "这里只保留最常回访的页面；第一次使用的完整解释请看“使用引导”。":
            "Only the pages you are most likely to revisit are kept here. For the full first-run explanation, use the Guide page.",
        "先把输出目录、Zotero 路径和基础 setup 定下来。":
            "Start by confirming the output directory, Zotero paths, and the basic setup.",
        "真正的输入来源、运行方式和主流程按钮都在这里。":
            "The real input-source controls, run modes, and primary workflow buttons are all here.",
        "最近一次 job workspace、主要产物和日志入口都集中在这里。":
            "The latest job workspace, primary artifacts, and log entry points are grouped here.",
        "输入方式、运行策略、OCR / MinerU、复用和工作区的完整说明都在这一页。":
            "This page contains the full explanation for input modes, run strategies, OCR / MinerU, reuse, and workspaces.",
        "5. MinerU token 在“API 与模型”页填写；真正是否调用，要到“性能与预处理”页选择解析策略。":
            "5. Fill in the MinerU token on the APIs & Models page; decide whether it is actually used on the Performance & Preprocessing page.",
        "阅读 / 写作 / 大纲 / 自由模式 / 验证模型都在这里配置；MinerU 远程解析的 token 也放在这里统一管理。":
            "Reader / writer / outline / free-mode / validator models are configured here, and the MinerU remote token is managed here as well.",
        "MinerU 远程解析": "MinerU Remote Parsing",
        "这是 PDF 预处理使用的远程解析后端，不属于 LLM 模型卡。是否真的调用，还取决于“性能与预处理”页里的解析策略。":
            "This is the remote parsing backend used by PDF preprocessing, not an LLM model card. Whether it is actually called still depends on the parser strategy on the Performance & Preprocessing page.",
        "API Token": "API Token",
        "模型版本": "Model Version",
        "MinerU token 已填写。": "MinerU token is configured.",
        "MinerU token 还没有填写。": "MinerU token is not configured yet.",
        "当前 parser mode 是 local：即使保存了 MinerU token，运行时也只会走本地解析链。":
            "The current parser mode is local: even if you save a MinerU token, runtime still uses only the local parsing chain.",
        "当前 parser mode 是 hybrid：系统会先跑本地基线，只有质量不佳时才会尝试 MinerU。":
            "The current parser mode is hybrid: the app runs a local baseline first and only tries MinerU when the quality looks weak.",
        "当前 parser mode 是 hybrid，但还没有 MinerU token，因此最终仍只会保留本地解析。":
            "The current parser mode is hybrid, but there is still no MinerU token, so the run will stay local in practice.",
        "当前 parser mode 是 remote：会直接请求 MinerU；远程失败时仍允许回退到本地解析。":
            "The current parser mode is remote: it requests MinerU directly, and a local fallback is still allowed if the remote step fails.",
        "当前 parser mode 是 remote：会直接请求 MinerU；你已经关闭本地回退，所以远程不可用时会直接失败。":
            "The current parser mode is remote: it requests MinerU directly, and local fallback is disabled, so remote unavailability will fail the run.",
        "当前 parser mode 是 remote，但还没有 MinerU token，因此远程解析不会真正发起。":
            "The current parser mode is remote, but there is still no MinerU token, so remote parsing will not actually start.",
        "当前 parser mode 是 remote_first：会优先尝试 MinerU，失败后允许切回本地解析。":
            "The current parser mode is remote_first: it tries MinerU first and may fall back to local parsing if needed.",
        "当前 parser mode 是 remote_first：会优先尝试 MinerU；你已经关闭本地回退，所以远程失败时会直接终止。":
            "The current parser mode is remote_first: it tries MinerU first, and local fallback is disabled, so a remote failure stops the run.",
        "当前 parser mode 是 remote_first，但还没有 MinerU token，因此最终仍会落回本地解析。":
            "The current parser mode is remote_first, but there is still no MinerU token, so the run will still end up on the local path.",
        "这一页专门控制并发、解析策略、PDF 预处理、OCR 和本地 RAG。MinerU 是否真正启用，也在这里决定。":
            "This page controls concurrency, parser strategy, PDF preprocessing, OCR, and local RAG. Whether MinerU is truly enabled is also decided here.",
        "解析策略与 MinerU": "Parser Strategy & MinerU",
        "MinerU 会不会真正用上，取决于这里的 parser mode、主解析器和回退策略，而不只是有没有填 token。":
            "Whether MinerU is actually used depends on the parser mode, primary parser, and fallback strategy here—not just on whether a token exists.",
        "Parser mode": "Parser mode",
        "主解析器": "Primary Parser",
        "回退解析器": "Fallback Parser",
        "允许本地回退": "Allow local fallback",
        "local · 仅本地": "local · local only",
        "hybrid · 先本地后判定": "hybrid · local first, then decide",
        "remote_first · 先尝试 MinerU": "remote_first · try MinerU first",
        "remote · 只走 MinerU": "remote · MinerU only",
        "local · 本地解析链": "local · local parser chain",
        "mineru_remote · MinerU 远程": "mineru_remote · MinerU remote",
        "这页保留第一次使用所需的完整说明：输入来源、运行方式、OCR / MinerU、复用和工作区应该怎么理解。":
            "This page keeps the full first-run explanation: how to think about input sources, run modes, OCR / MinerU, reuse, and workspaces.",
        "关于 OCR、MinerU、复用和工作区": "About OCR, MinerU, Reuse, and Workspaces",
        "MinerU 也不是默认常开：只有 parser mode 请求远程，且 hybrid 判定本地质量不足时，才会真正发起远程解析。":
            "MinerU is not always-on by default either: the remote call happens only when the parser mode requests it, and in hybrid mode only when the local baseline looks insufficient.",
    }
)


EN_TO_ZH = {
    "Task input": "任务输入",
    "Choose the PDF folder or Zotero report first, then fill in the project name and any reuse settings.": "先选择 PDF 文件夹或 Zotero 报告，再填写项目名和复用设置。",
    "Choose the PDF folder or Zotero report first, then fill in the project name. Stage-1 reuse now scans historical outputs automatically, and manual summary paths are only needed for advanced cases.": "先选择 PDF 文件夹或 Zotero 报告，再填写项目名。第一阶段复用现在会自动扫描历史输出；手动填写 summary 路径只用于高级场景。",
    "Project name": "项目名",
    "PDF folder": "PDF 文件夹",
    "Select PDF folder": "选择 PDF 文件夹",
    "Zotero report": "Zotero 报告",
    "Select Zotero report file": "选择 Zotero 报告文件",
    "Summary file": "摘要文件",
    "Select summaries.json file": "选择 summaries.json 文件",
    "Auto reuse historical stage-1 summaries": "自动复用历史第一阶段摘要",
    "When enabled, stage 1 scans all historical project outputs plus compatible legacy summaries under the configured output path, then only analyzes the papers that are still missing.": "启用后，第一阶段会自动扫描 output_path 下的所有历史项目结果和兼容旧版摘要，只分析当前仍然缺失的论文。",
    "Advanced summary source options": "高级摘要来源选项",
    "Additional downstream summary sources (one path per line)": "额外的下游摘要来源（每行一个路径）",
    "Additional reuse summary files (one path per line)": "额外的复用摘要文件（每行一个路径）",
    "Additional stage-1 reuse summary files outside output_path (one path per line)": "位于 output_path 之外的第一阶段复用 summary 文件（每行一个路径）",
    "Open PDF folder": "打开 PDF 文件夹",
    "Open Zotero report": "打开 Zotero 报告",
    "Open summary file": "打开摘要文件",
    "Open Setup": "打开 Setup 页面",
    "Please enter a project name first.": "请先填写项目名。",
    "Please provide either a PDF folder or a Zotero report.": "请提供 PDF 文件夹或 Zotero 报告路径。",
    "Added {project_name} to the queue draft.": "已将 {project_name} 加入队列草稿。",
    "Removed {project_name} from the queue draft.": "已从队列草稿中移除 {project_name}。",
    "Cleared queue draft.": "已清空队列草稿。",
    "Queue draft is empty.": "队列草稿为空。",
    "Committed {count} drafted job(s) to the queue.": "已将 {count} 个草稿任务提交到队列。",
    "Added job to queue: {job_id}": "任务已加入队列：{job_id}",
    "Failed to add job to queue: {error}": "任务入队失败：{error}",
}

EN_TO_ZH.update(
    {
        "API Token": "API Token",
        "Parser mode": "解析模式",
        "Model Version": "模型版本",
        "Parser Strategy": "解析策略",
        "Current Workspace Snapshot": "当前工作台快照",
        "Common Pages": "常用入口",
        "Primary Parser": "主解析器",
        "Fallback Parser": "回退解析器",
        "Allow local fallback": "允许本地回退",
        "local · local only": "local · 仅本地",
        "hybrid · local first, then decide": "hybrid · 先本地后判定",
        "remote_first · try MinerU first": "remote_first · 先尝试 MinerU",
        "remote · MinerU only": "remote · 只走 MinerU",
        "local · local parser chain": "local · 本地解析链",
        "mineru_remote · MinerU remote": "mineru_remote · MinerU 远程",
    }
)


def translate(language: str, key: str) -> str:
    if language == "zh-CN":
        return EN_TO_ZH.get(key, key)
    return TRANSLATIONS.get(language, {}).get(key, key)


def action_label(language: str, action: str) -> str:
    return ACTION_LABELS.get(language, ACTION_LABELS["zh-CN"]).get(action, action)
