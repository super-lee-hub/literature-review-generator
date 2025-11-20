"""
Word文档生成模块
负责创建和格式化Word文档，包括样式配置、目录生成和参考文献格式化
"""

import os
import re
from typing import Optional, Any, Dict, List  # type: ignore
from docx import Document  # type: ignore
from docx.shared import Pt, Inches, Cm  # type: ignore
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT  # type: ignore
from docx.oxml.ns import qn  # type: ignore
from docx.oxml import OxmlElement  # type: ignore
from docx.oxml.ns import qn  # type: ignore


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


def append_section_to_word_document(generator_instance: Any, section_number: int, section_title: str, section_text: str, word_file: str) -> bool:
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
        heading.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT
        
        # 应用标题样式配置
        for run in heading.runs:
            run.font.name = font_name
            run.font.size = Pt(font_size_heading2)
        
        # 添加章节内容
        # 将文本按段落分割
        paragraphs = section_text.split('\n\n')
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


def generate_apa_references(generator_instance: Any) -> List[str]:
    """
    生成APA格式的参考文献列表
    
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
            
            # 格式化作者（即使没有作者信息也要生成参考文献）
            if authors:
                if len(authors) <= 7:
                    author_list: str = ', '.join(authors)
                else:
                    author_list: str = ', '.join(authors[:6]) + ', ..., ' + authors[-1]
            else:
                # 没有作者信息时使用占位符
                author_list: str = "Anonymous"
            
            # 构建引用字符串，即使部分信息缺失也继续
            ref_parts: List[str] = [author_list]
            ref_parts.append(f"({year or 'n.d.'}).")
            ref_parts.append(f"{title or '无标题'}.")
            if journal:
                ref_parts.append(f"*{journal}*")
            if doi:
                ref_parts.append(f"https://doi.org/{doi}")

            references.append(" ".join(ref_parts))
        
        # 按第一作者姓氏排序
        references.sort(key=lambda x: x.split(',')[0] if ',' in x else x)
        
        return references
        
    except Exception as e:
        generator_instance.logger.error(f"生成APA参考文献失败: {e}")
        return []


def create_word_document(generator_instance: Any, markdown_text: str, output_path: str) -> bool:
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
        generator_instance.logger.info("正在生成Word文档...")
        
        # 创建新的Word文档
        doc = Document()
        
        # 加载样式配置
        style_config: Dict[str, Any] = generator_instance.config.get('Styling') or {}
        font_name: str = style_config.get('font_name', 'Times New Roman')
        font_size_body: int = int(style_config.get('font_size_body', '12'))
        font_size_heading1: int = int(style_config.get('font_size_heading1', '16'))
        font_size_heading2: int = int(style_config.get('font_size_heading2', '14'))
        
        # 设置高级文档样式
        set_advanced_document_styles(doc, font_name, font_size_body, font_size_heading1, font_size_heading2)
        
        # 添加页眉页脚和页码
        add_header_and_footer(doc, "文献综述")
        
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
        generator_instance.logger.success(f"Word文档已生成: {output_path}")
        return True
        
    except Exception as e:
        generator_instance.logger.error(f"创建Word文档失败: {e}")
        return False