# file: models.py

"""
核心数据模型定义文件
使用 TypedDict 定义项目中的所有核心字典结构
这是整个项目的「单一事实来源 (Single Source of Truth)」
"""

from typing import TypedDict, List, Optional, Dict, Any
from typing_extensions import NotRequired

# --- 核心数据结构 ---

class PaperInfo(TypedDict, total=False):
    """从 Zotero 或 PDF 文件解析出的原始论文元数据。所有键都是可选的，以适应不同来源。"""
    title: str
    authors: List[str]
    year: str
    journal: str
    doi: str
    attachments: List[str]
    pdf_path: str
    file_index: int
    item_type: str
    abstract: str
    # ... and other potential keys from zotero_parser
    publication_title: str
    volume: str
    issue: str
    pages: str

class CommonCoreSummary(TypedDict):
    """AI 分析结果的核心部分"""
    summary: str
    key_points: List[str]
    methodology: str
    findings: str
    conclusions: str
    relevance: str
    limitations: str

class ConceptAnalysis(TypedDict, total=False):
    """概念增强分析的结果 (所有键都是可选的)"""
    contribution_to_concept: str
    position_in_development: str
    novelty_or_confirmation: str

class AISummary(TypedDict):
    """完整的 AI 分析结果，包含两段式结构"""
    common_core: CommonCoreSummary
    type_specific_details: Dict[str, Any]
    concept_analysis: NotRequired[Optional[ConceptAnalysis]]

class ProcessingResult(TypedDict):
    """单篇论文处理完成后的最终结果对象 (成功或失败)"""
    paper_info: PaperInfo
    status: str  # 'success' or 'failed'
    ai_summary: NotRequired[Optional[AISummary]]
    processing_time: NotRequired[Optional[str]]
    failure_reason: NotRequired[Optional[str]]

class FailedPaper(TypedDict):
    """失败论文的记录结构"""
    paper_info: PaperInfo
    failure_reason: str

# --- 配置相关类型 ---

class APIConfig(TypedDict):
    api_key: Optional[str]
    model: Optional[str]
    api_base: Optional[str]

# --- 辅助类型 ---
SummariesList = List[ProcessingResult]