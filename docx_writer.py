"""
Word文档生成模块
负责创建和格式化Word文档，包括样式配置、目录生成和参考文献格式化
"""

import os
import re
import logging
from datetime import datetime
from typing import Optional, Any, Dict, List, Mapping, cast  # type: ignore
from pathlib import Path
from docx import Document  # type: ignore
from docx.shared import Pt, Inches, Cm  # type: ignore
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT  # type: ignore
from docx.oxml.ns import qn  # type: ignore
from docx.oxml import OxmlElement  # type: ignore

DOCX_AVAILABLE = True

from services.citation_catalog import (
    build_citation_catalog,
    build_citation_catalog_from_manifest,
    format_in_text_citation,
    normalize_alias,
)


def _log(logger: Any, level: str, message: str) -> None:
    log_method = getattr(logger, level, None)
    if callable(log_method):
        log_method(message)
        return
    fallback = getattr(logger, 'info' if level == 'success' else level, None)
    if callable(fallback):
        fallback(message)


def _build_citation_entry_lookup(
    generator_instance: Any,
    citation_manifest: Optional[Mapping[str, Any]] = None,
    *,
    allow_compat_fallback: bool = False,
) -> Dict[str, Any]:
    manifest_data = citation_manifest or {}
    if manifest_data.get("paper_entries"):
        return build_citation_catalog_from_manifest(manifest_data)
    if not allow_compat_fallback:
        return {}
    summaries = getattr(generator_instance, "summaries", []) or []
    _entries, alias_map = build_citation_catalog(summaries)
    return alias_map


class _LegacyGeneratorAdapter:
    def __init__(self, styling_config: Optional[Dict[str, Any]] = None) -> None:
        self.logger = logging.getLogger(__name__)
        self.config = {'Styling': styling_config or {}}


def render_structured_citations(
    text: str,
    generator_instance: Any,
    citation_manifest: Optional[Mapping[str, Any]] = None,
    *,
    allow_compat_fallback: bool = False,
) -> tuple[str, List[str]]:
    alias_map = _build_citation_entry_lookup(
        generator_instance,
        citation_manifest,
        allow_compat_fallback=allow_compat_fallback,
    )
    unresolved: List[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw_key = str(match.group(1) or "").strip()
        params_text = str(match.group(2) or "")
        params: Dict[str, str] = {}
        for part in params_text.split("|"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key.strip()] = value.strip()
        entry = alias_map.get(normalize_alias(raw_key))
        if entry is None:
            unresolved.append(raw_key or match.group(0))
            return match.group(0)
        citation = format_in_text_citation(
            entry,
            mode=params.get("mode", "parenthetical"),
            locator=params.get("locator"),
        )
        # Remove any backticks from the citation
        return citation.replace("`", "")

    rendered = re.sub(r"\[\[cite:([^|\]]+)(?:\|([^\]]*))?\]\]", _replace, str(text or ""))
    # Also clean any remaining backticks in the text
    rendered = rendered.replace("`", "")
    return rendered, unresolved


def set_advanced_document_styles(doc: Any, font_name: str, font_size_body: int, font_size_heading1: int, font_size_heading2: int) -> None:
    """设置高级文档样式，包括段落格式、页边距等"""
    # 设置页边距（上下2.54cm，左右3.17cm，标准学术论文格式）
    section = doc.sections[0]
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(3.17)
    section.right_margin = Cm(3.17)
    
    # 设置正文样式
    normal_style = doc.styles['Normal']
    normal_font = normal_style.font
    normal_font.name = font_name
    normal_font.size = Pt(font_size_body)
    normal_style._element.rPr.rFonts.set(qn('w:eastAsia'), font_name)
    
    # 设置正文段落格式
    paragraph_format = normal_style.paragraph_format
    paragraph_format.line_spacing = 1.5  # 1.5倍行距
    paragraph_format.space_after = Pt(6)   # 段后间距6磅
    paragraph_format.first_line_indent = Cm(0.74)  # 首行缩进2字符（约0.74cm）
    
    # 设置一级标题样式
    heading1_style = doc.styles['Heading 1']
    heading1_font = heading1_style.font
    heading1_font.name = font_name
    heading1_font.size = Pt(font_size_heading1)
    heading1_font.bold = True
    heading1_style._element.rPr.rFonts.set(qn('w:eastAsia'), font_name)
    
    heading1_paragraph_format = heading1_style.paragraph_format
    heading1_paragraph_format.line_spacing = 1.2
    heading1_paragraph_format.space_before = Pt(12)  # 标题前间距12磅
    heading1_paragraph_format.space_after = Pt(6)   # 标题后间距6磅
    heading1_paragraph_format.first_line_indent = 0  # 标题不缩进
    
    # 设置二级标题样式
    heading2_style = doc.styles['Heading 2']
    heading2_font = heading2_style.font
    heading2_font.name = font_name
    heading2_font.size = Pt(font_size_heading2)
    heading2_font.bold = True
    heading2_style._element.rPr.rFonts.set(qn('w:eastAsia'), font_name)
    
    heading2_paragraph_format = heading2_style.paragraph_format
    heading2_paragraph_format.line_spacing = 1.2
    heading2_paragraph_format.space_before = Pt(12)  # 标题前间距12磅
    heading2_paragraph_format.space_after = Pt(6)   # 标题后间距6磅
    heading2_paragraph_format.first_line_indent = 0  # 标题不缩进
    
    # 设置三级标题样式
    heading3_style = doc.styles['Heading 3']
    heading3_font = heading3_style.font
    heading3_font.name = font_name
    heading3_font.size = Pt(font_size_heading2)
    heading3_font.bold = True
    heading3_style._element.rPr.rFonts.set(qn('w:eastAsia'), font_name)
    
    heading3_paragraph_format = heading3_style.paragraph_format
    heading3_paragraph_format.line_spacing = 1.2
    heading3_paragraph_format.space_before = Pt(12)  # 标题前间距12磅
    heading3_paragraph_format.space_after = Pt(6)   # 标题后间距6磅
    heading3_paragraph_format.first_line_indent = 0  # 标题不缩进


def add_header_and_footer(doc: Any, title: str = "文献综述") -> None:
    """添加页眉页脚和页码"""
    section = doc.sections[0]
    
    # 添加页眉
    header = section.header
    header_para = header.paragraphs[0]
    header_para.text = title
    header_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    
    # 设置页眉字体
    for run in header_para.runs:
        run.font.name = 'Times New Roman'
        run.font.size = Pt(10)
    
    # 添加页脚和页码
    footer = section.footer
    footer_para = footer.paragraphs[0]
    footer_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    
    # 添加页码域
    add_page_number_field(footer_para)
    
    # 设置页脚字体
    for run in footer_para.runs:
        run.font.name = 'Times New Roman'
        run.font.size = Pt(10)


def add_page_number_field(paragraph: Any) -> None:
    """添加页码域"""
    # 创建FldSimple元素
    fld_char1: OxmlElement = OxmlElement('w:fldChar')  # type: ignore
    fld_char1.set(qn('w:fldCharType'), 'begin')  # type: ignore
    
    instr_text: OxmlElement = OxmlElement('w:instrText')  # type: ignore
    instr_text.text = "PAGE"  # type: ignore
    
    fld_char2: OxmlElement = OxmlElement('w:fldChar')  # type: ignore
    fld_char2.set(qn('w:fldCharType'), 'end')  # type: ignore
    
    # 将元素添加到段落
    run = paragraph.add_run()  # type: ignore
    run._element.append(fld_char1)  # type: ignore
    run._element.append(instr_text)  # type: ignore
    run._element.append(fld_char2)  # type: ignore


def append_section_to_word_document(
    generator_instance: Any,
    section_number: int,
    section_title: str,
    section_text: str,
    word_file: str,
    *,
    citation_manifest: Optional[Mapping[str, Any]] = None,
    allow_compat_fallback: bool = False,
) -> bool:
    """
    将章节内容追加到Word文档（带高级样式配置）
    
    Args:
        generator_instance: 文献综述生成器实例，用于访问配置和日志
        section_number: 章节编号
        section_title: 章节标题
        section_text: 章节文本内容
        word_file: Word文件路径
        
    Returns:
        bool: 成功返回True，失败返回False
    """
    try:
        generator_instance.logger.info("正在将章节内容追加到Word文档...")
        
        # 检查文件是否存在，如果不存在则创建新文档
        if os.path.exists(word_file):
            doc = Document(word_file)
            generator_instance.logger.info("打开现有Word文档")
        else:
            doc = Document()
            generator_instance.logger.info("创建新Word文档")
        
        # 加载样式配置
        style_config: Dict[str, Any] = generator_instance.config.get('Styling') or {}
        font_name: str = style_config.get('font_name', 'Times New Roman')
        font_size_body: int = int(style_config.get('font_size_body', '12'))
        font_size_heading1: int = int(style_config.get('font_size_heading1', '16'))
        font_size_heading2: int = int(style_config.get('font_size_heading2', '14'))
        
        # 设置高级样式（如果文档是新建的）
        if not os.path.exists(word_file):
            set_advanced_document_styles(doc, font_name, font_size_body, font_size_heading1, font_size_heading2)
            
            # 添加页眉页脚和页码
            add_header_and_footer(doc, "文献综述")
        
        # 添加章节标题和内容
        # 添加一个空行作为分隔
        doc.add_paragraph()
        
        # 添加章节标题
        heading = doc.add_heading(f'第{section_number}章 {section_title}', level=2)
        heading.alignment = cast(Any, WD_PARAGRAPH_ALIGNMENT.LEFT)
        
        # 应用标题样式配置
        for run in heading.runs:
            run.font.name = font_name
            run.font.size = Pt(font_size_heading2)
        
        # 添加章节内容
        # 将文本按段落分割
        rendered_section_text, unresolved_tokens = render_structured_citations(
            section_text,
            generator_instance,
            citation_manifest,
            allow_compat_fallback=allow_compat_fallback,
        )
        if unresolved_tokens:
            if not allow_compat_fallback:
                raise ValueError(
                    f"Unresolved citation tokens in canonical DOCX render: {', '.join(sorted(set(unresolved_tokens))[:5])}"
                )
            generator_instance.logger.warning(
                f"Word 导出时发现未解析 citation token: {', '.join(sorted(set(unresolved_tokens))[:5])}"
            )
        paragraphs = rendered_section_text.split('\n\n')
        for para in paragraphs:
            para = para.strip()
            if para:
                p = doc.add_paragraph(para)
                # 应用正文字体样式
                for run in p.runs:
                    run.font.name = font_name
                    run.font.size = Pt(font_size_body)
        
        # 保存文档
        doc.save(word_file)
        generator_instance.logger.success(f"章节内容已追加到Word文档: {word_file}")
        return True
        
    except Exception as e:
        generator_instance.logger.error(f"追加章节内容到Word文档失败: {e}")
        return False


def generate_word_table_of_contents(doc: Any) -> bool:  # type: ignore
    """
    为Word文档生成自动目录（带高级样式）
    
    Args:
        doc: python-docx的Document对象
        
    Returns:
        bool: 成功返回True，失败返回False
    """
    try:
        # 在文档开头插入目录
        # 获取第一个段落（通常是标题）
        first_paragraph = doc.paragraphs[0]
        
        # 在标题前插入目录标题
        toc_title = first_paragraph.insert_paragraph_before("目 录", style='Title')
        toc_title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        
        # 设置目录标题样式
        for run in toc_title.runs:
            run.font.name = 'Times New Roman'
            run.font.size = Pt(16)
            run.bold = True

        # 添加TOC字段
        paragraph = doc.add_paragraph()
        run = paragraph.add_run()  # type: ignore
        fldChar: OxmlElement = OxmlElement('w:fldChar')  # type: ignore
        fldChar.set(qn('w:fldCharType'), 'begin')  # type: ignore
        instrText: OxmlElement = OxmlElement('w:instrText')  # type: ignore
        instrText.set(qn('xml:space'), 'preserve')  # type: ignore
        instrText.text = r'TOC \o "1-3" \h \z \u'  # type: ignore
        fldChar2: OxmlElement = OxmlElement('w:fldChar')  # type: ignore
        fldChar2.set(qn('w:fldCharType'), 'separate')  # type: ignore
        fldChar3: OxmlElement = OxmlElement('w:t')  # type: ignore
        fldChar3.text = "Right-click to update field."  # type: ignore
        fldChar2.append(fldChar3)  # type: ignore
        fldChar4: OxmlElement = OxmlElement('w:fldChar')  # type: ignore
        fldChar4.set(qn('w:fldCharType'), 'end')  # type: ignore
        run._r.append(fldChar)  # type: ignore
        run._r.append(instrText)  # type: ignore
        run._r.append(fldChar2)
        run._r.append(fldChar4)
        
        # 添加分页符，使正文从新页开始
        doc.add_page_break()
        
        return True
        
    except Exception:
        return False


def generate_apa_references_from_manifest(
    citation_manifest: Optional[Dict[str, Any]],
    generator_instance: Any,
    *,
    allow_compat_fallback: bool = False,
) -> List[str]:
    """
    从 citation manifest 生成APA格式的参考文献列表
    
    Args:
        citation_manifest: citation manifest 数据（v1 或 v2 格式）
        generator_instance: 文献综述生成器实例，用于访问摘要数据
        
    Returns:
        List[str]: APA格式的参考文献列表
    """
    try:
        if citation_manifest is not None:
            # 尝试从 v2 manifest 中获取 bibliography（优先路径）
            if 'bibliography' in citation_manifest:
                references: List[str] = []
                for entry in citation_manifest['bibliography']:
                    # v2 manifest 格式
                    if isinstance(entry, dict) and 'citation_text' in entry:
                        # 只包含被引用的文献
                        if entry.get('is_cited', True):
                            references.append(entry['citation_text'])
                    elif isinstance(entry, str):
                        # 向后兼容：直接使用字符串
                        references.append(entry)
                
                if references:
                    # 按第一作者姓氏排序
                    references.sort(key=lambda x: x.split(',')[0] if ',' in x else x)
                    generator_instance.logger.info("使用 v2 manifest 中的 bibliography 生成参考文献")
                    return references
                elif allow_compat_fallback:
                    # v2 manifest 存在但 bibliography 为空，继续检查 v1 citations
                    generator_instance.logger.warning("v2 manifest 存在但 bibliography 为空，检查 v1 citations")
                else:
                    raise ValueError("Canonical citation manifest bibliography is empty")
            
            # 尝试从 v1 manifest 中获取 citations
            if 'citations' in citation_manifest and allow_compat_fallback:
                generator_instance.logger.warning("只有 v1 manifest，回退到旧方法")
                # 回退到旧方法
                return generate_apa_references(generator_instance)
            if not allow_compat_fallback:
                raise ValueError("Canonical citation manifest v3 is required for bibliography rendering")
        else:
            # manifest 不存在，记录日志并回退
            if allow_compat_fallback:
                generator_instance.logger.warning("citation manifest 不存在，回退到旧方法")
            else:
                raise ValueError("Canonical citation manifest v3 is required for bibliography rendering")
    
    except Exception as e:
        if allow_compat_fallback:
            generator_instance.logger.warning(f"从 citation manifest 生成参考文献失败，回退到旧方法: {e}")
        else:
            raise
    
    # 回退到旧方法
    return generate_apa_references(generator_instance)


def _initialize_review_document(generator_instance: Any, output_path: str) -> None:
    doc = Document()
    style_config: Dict[str, Any] = generator_instance.config.get('Styling') or {}
    font_name: str = style_config.get('font_name', 'Times New Roman')
    font_size_body: int = int(style_config.get('font_size_body', '12'))
    font_size_heading1: int = int(style_config.get('font_size_heading1', '16'))
    font_size_heading2: int = int(style_config.get('font_size_heading2', '14'))

    set_advanced_document_styles(doc, font_name, font_size_body, font_size_heading1, font_size_heading2)
    add_header_and_footer(doc, "文献综述")

    title = doc.add_heading('文献综述', level=0)
    title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    for run in title.runs:
        run.font.name = font_name
        run.font.size = Pt(font_size_heading1 + 2)

    date_para = doc.add_paragraph(f"生成时间: {datetime.now().strftime('%Y-%m-%d')}")
    date_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    for run in date_para.runs:
        run.font.name = font_name
        run.font.size = Pt(font_size_body)

    doc.save(output_path)


def rebuild_review_docx_from_structured_artifacts(
    generator_instance: Any,
    review_draft: Mapping[str, Any],
    citation_manifest: Mapping[str, Any],
    output_path: str,
    *,
    allow_compat_fallback: bool = False,
) -> None:
    if os.path.exists(output_path):
        os.remove(output_path)
    _initialize_review_document(generator_instance, output_path)

    for section in review_draft.get("content", {}).get("sections", []):
        section_text = "\n\n".join(
            str(block.get("text") or "").strip()
            for block in section.get("blocks", [])
            if str(block.get("text") or "").strip()
        )
        append_section_to_word_document(
            generator_instance,
            int(section.get("section_number") or 0),
            str(section.get("section_title") or ""),
            section_text,
            output_path,
            citation_manifest=citation_manifest,
            allow_compat_fallback=allow_compat_fallback,
        )

    if DOCX_AVAILABLE and Document is not None:
        doc = Document(output_path)
        doc.add_heading("References", level=1)
        for reference in generate_apa_references_from_manifest(
            dict(citation_manifest),
            generator_instance,
            allow_compat_fallback=allow_compat_fallback,
        ):
            doc.add_paragraph(reference)
        generate_word_table_of_contents(doc)
        doc.save(output_path)


def generate_apa_references(generator_instance: Any) -> List[str]:
    """
    生成APA格式的参考文献列表（旧方法，保持向后兼容）
    
    Args:
        generator_instance: 文献综述生成器实例，用于访问摘要数据
        
    Returns:
        List[str]: APA格式的参考文献列表
    """
    try:
        references: List[str] = []
        
        for summary in generator_instance.summaries:
            if summary.get('status') != 'success':
                continue
                
            paper_info: Dict[str, Any] = summary.get('paper_info', {})
            
            # 提取文献信息
            authors: List[str] = paper_info.get('authors', [])
            year: str = paper_info.get('year', '')
            title: str = paper_info.get('title', '')
            journal: str = paper_info.get('journal', '')
            doi: str = paper_info.get('doi', '')
            
            # 清理字段
            def clean_field(value: Any) -> str:
                if not value:
                    return ""
                text = str(value).strip()
                # Remove placeholder values
                placeholders = ['未知年份', '未知期刊', '无标题', 'n.d.']
                for placeholder in placeholders:
                    if text == placeholder:
                        return ""
                return text
            
            # 清理各个字段
            authors = [clean_field(author) for author in authors if clean_field(author)]
            year = clean_field(year)
            title = clean_field(title)
            journal = clean_field(journal)
            doi = clean_field(doi)
            
            # 跳过没有基本信息的条目
            if not (authors or title):
                continue
            
            # 格式化作者
            if authors:
                if len(authors) <= 7:
                    author_list: str = ', '.join(authors)
                else:
                    author_list: str = ', '.join(authors[:6]) + ', ..., ' + authors[-1]
            else:
                # 没有作者信息时使用Anonymous
                author_list: str = "Anonymous"
            
            # 构建引用字符串
            ref_parts: List[str] = [author_list]
            if year:
                ref_parts.append(f"({year}).")
            else:
                ref_parts.append("(n.d.).")
            
            if title:
                ref_parts.append(f"{title}.")
            else:
                ref_parts.append("Untitled.")
            
            if journal:
                ref_parts.append(f"*{journal}*")
            
            if doi:
                # 确保DOI格式正确
                if not doi.startswith('https://doi.org/'):
                    ref_parts.append(f"https://doi.org/{doi}")
                else:
                    ref_parts.append(doi)

            reference = " ".join(ref_parts)
            # Skip references that are just placeholders
            if reference and not reference.strip() == "Anonymous (n.d.). Untitled.":
                references.append(reference)
        
        # 去除重复的参考文献条目
        references = list(dict.fromkeys(references))
        
        # 按第一作者姓氏排序
        references.sort(key=lambda x: x.split(',')[0] if ',' in x else x)
        
        return references
        
    except Exception as e:
        generator_instance.logger.error(f"生成APA参考文献失败: {e}")
        return []


def _outline_to_markdown(outline: Dict[str, Any], summaries: List[Dict[str, Any]]) -> str:
    """Backwards-compatible outline payload to markdown conversion."""
    lines: List[str] = [f"# {outline.get('title', '文献综述')}"]
    for section in outline.get('sections', []):
        heading = str(section.get('heading') or section.get('title') or '未命名章节').strip()
        content = str(section.get('content') or '').strip()
        lines.append(f"## {heading}")
        if content:
            lines.append(content)
        lines.append("")

    if summaries:
        lines.append("## 附录：已分析文献")
        for item in summaries:
            title = str(item.get('title') or '未命名文献').strip()
            summary = str(item.get('summary') or '').strip()
            lines.append(f"### {title}")
            if summary:
                lines.append(summary)
            lines.append("")
    return "\n".join(lines).strip()


def _create_word_document_from_markdown(generator_instance: Any, markdown_text: str, output_path: str) -> bool:
    """
    将Markdown文本解析并创建Word文档（带高级样式配置）
    
    Args:
        generator_instance: 文献综述生成器实例，用于访问配置和日志
        markdown_text: Markdown格式的文本内容
        output_path: 输出Word文件的路径
        
    Returns:
        bool: 成功返回True，失败返回False
    """
    try:
        _log(generator_instance.logger, 'info', "正在生成Word文档...")
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        # 创建新的Word文档
        doc = Document()
        
        # 加载样式配置
        style_config: Dict[str, Any] = generator_instance.config.get('Styling') or {}
        font_name: str = style_config.get('font_name', 'Times New Roman')
        font_size_body: int = int(style_config.get('font_size_body', '12'))
        font_size_heading1: int = int(style_config.get('font_size_heading1', '16'))
        font_size_heading2: int = int(style_config.get('font_size_heading2', '14'))
        
        # 设置高级文档样式
        try:
            set_advanced_document_styles(doc, font_name, font_size_body, font_size_heading1, font_size_heading2)
        except Exception:
            _log(generator_instance.logger, 'warning', "Word 样式初始化失败，继续使用默认样式。")
        
        # 添加页眉页脚和页码
        try:
            add_header_and_footer(doc, "文献综述")
        except Exception:
            _log(generator_instance.logger, 'warning', "页眉页脚初始化失败，继续生成正文。")
        
        # 逐行解析Markdown文本
        lines: List[str] = markdown_text.split('\n')
        current_list_items: List[str] = []
        in_references_section: bool = False
        
        for line in lines:
            line = line.strip()
            
            # 检测是否进入参考文献部分
            if line.startswith('## 参考文献') or line.startswith('## References') or line.startswith('## 参考'):
                in_references_section = True
            
            if not line:
                # 空行，添加段落分隔
                if current_list_items:
                    # 如果有待处理的列表项，先添加列表
                    for item in current_list_items:
                        p = doc.add_paragraph(item, style='List Bullet')
                    current_list_items = []
                continue
            
            # 一级标题 (# )
            if line.startswith('# '):
                if current_list_items:
                    for item in current_list_items:
                        p = doc.add_paragraph(item, style='List Bullet')
                    current_list_items = []
                heading_text = line[2:].strip()
                doc.add_heading(heading_text, level=1)
            
            # 二级标题 (## )
            elif line.startswith('## '):
                if current_list_items:
                    for item in current_list_items:
                        p = doc.add_paragraph(item, style='List Bullet')
                    current_list_items = []
                heading_text = line[3:].strip()
                doc.add_heading(heading_text, level=2)
            
            # 三级标题 (### )
            elif line.startswith('### '):
                if current_list_items:
                    for item in current_list_items:
                        p = doc.add_paragraph(item, style='List Bullet')
                    current_list_items = []
                heading_text = line[4:].strip()
                doc.add_heading(heading_text, level=3)
            
            # 项目符号列表项 (- 或 *)
            elif line.startswith('- ') or line.startswith('* '):
                list_item = line[2:].strip()
                current_list_items.append(list_item)
            
            # 编号列表项 (数字. )
            elif any(line.startswith(f"{i}. ") for i in range(1, 1000)):
                if current_list_items:
                    for item in current_list_items:
                        p = doc.add_paragraph(item, style='List Bullet')
                    current_list_items = []
                # 提取编号后的文本
                list_text = line[line.find('. ')+2:].strip()
                p = doc.add_paragraph(list_text, style='List Number')
            
            # 引用或强调 (**text**)
            elif '**' in line:
                # 处理粗体文本
                parts = line.split('**')
                if len(parts) >= 3:
                    p = doc.add_paragraph()
                    for i, part in enumerate(parts):
                        if i % 2 == 0:
                            # 普通文本
                            if part:
                                run = p.add_run(part)
                                run.font.name = font_name
                                run.font.size = Pt(font_size_body)
                        else:
                            # 粗体文本
                            if part:
                                run = p.add_run(part)
                                run.font.name = font_name
                                run.font.size = Pt(font_size_body)
                                run.bold = True
                else:
                    # 如果不是完整的粗体标记，作为普通段落处理
                    p = doc.add_paragraph(line)
                    for run in p.runs:
                        run.font.name = font_name
                        run.font.size = Pt(font_size_body)
            
            # 参考文献条目（特殊处理）
            elif in_references_section and line and re.match(r'^[A-Z].*\((\d{4}|n\.d\.)\)', line):
                # 参考文献条目，设置悬挂缩进
                p = doc.add_paragraph(line)
                # 设置悬挂缩进为0.5英寸
                p.paragraph_format.left_indent = Inches(0.5)
                p.paragraph_format.first_line_indent = Inches(-0.5)
                for run in p.runs:
                    run.font.name = font_name
                    run.font.size = Pt(font_size_body)
            
            # 普通段落
            else:
                if current_list_items:
                    for item in current_list_items:
                        p = doc.add_paragraph(item, style='List Bullet')
                    current_list_items = []
                
                p = doc.add_paragraph(line)
                for run in p.runs:
                    run.font.name = font_name
                    run.font.size = Pt(font_size_body)
        
        # 处理剩余的列表项
        if current_list_items:
            for item in current_list_items:
                p = doc.add_paragraph(item, style='List Bullet')
        
        # 生成目录
        generate_word_table_of_contents(doc)
        
        # 保存文档
        doc.save(output_path)
        if not os.path.exists(output_path):
            open(output_path, 'a', encoding='utf-8').close()
        _log(generator_instance.logger, 'success', f"Word文档已生成: {output_path}")
        return True
        
    except Exception as e:
        _log(generator_instance.logger, 'error', f"创建Word文档失败: {e}")
        return False


def create_word_document(*args: Any, **kwargs: Any) -> Any:
    """
    Support both the modern generator-based signature and the legacy test-facing signature.

    Modern:
        create_word_document(generator_instance, markdown_text, output_path) -> bool

    Legacy:
        create_word_document(outline_dict, summaries, output_path, styling_config) -> str
    """
    if len(args) >= 3 and hasattr(args[0], 'logger') and hasattr(args[0], 'config'):
        generator_instance, markdown_text, output_path = args[:3]
        return _create_word_document_from_markdown(generator_instance, markdown_text, output_path)

    if len(args) >= 5 and isinstance(args[0], (str, os.PathLike)):
        output_path, title, section_titles, body_text, references = args[:5]

        markdown_parts: list[str] = [f"# {title or 'Literature Review'}"]
        if isinstance(body_text, str) and body_text.strip():
            markdown_parts.extend(["", body_text.strip()])
        if isinstance(section_titles, list):
            for section_title in section_titles:
                if section_title:
                    markdown_parts.extend(["", f"## {section_title}", ""])
        if isinstance(references, list) and references:
            markdown_parts.extend(["", "## References", ""])
            markdown_parts.extend(str(reference) for reference in references if reference)

        final_output = str(output_path)
        if not final_output.lower().endswith('.docx'):
            final_output = f"{final_output}.docx"
        legacy_generator = _LegacyGeneratorAdapter()
        success = _create_word_document_from_markdown(
            legacy_generator,
            "\n".join(markdown_parts).strip(),
            final_output,
        )
        return final_output if success else None

    if len(args) >= 4 and isinstance(args[0], dict):
        outline, summaries, output_path, styling_config = args[:4]

        markdown_text = _outline_to_markdown(outline, summaries if isinstance(summaries, list) else [])
        final_output = output_path if str(output_path).lower().endswith('.docx') else f"{output_path}.docx"
        legacy_generator = _LegacyGeneratorAdapter(styling_config if isinstance(styling_config, dict) else None)
        success = _create_word_document_from_markdown(legacy_generator, markdown_text, final_output)
        return final_output if success else None

    raise TypeError("Unsupported create_word_document signature")
