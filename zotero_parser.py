import logging
import re
from typing import List
from pathlib import Path
import os

from models import PaperInfo  # type: ignore

# 设置日志记录器
logger = logging.getLogger(__name__)


def parse_zotero_report(filepath: str) -> List[PaperInfo]:
    """
    增强的Zotero报告解析函数，支持多种格式：
    1. 标准Zotero报告格式
    2. 简化的键值对格式（用于重跑报告）
    3. 使用正则表达式的高级解析（新增强）

    Args:
        filepath: Zotero报告文件的路径。

    Returns:
        一个包含所有文献信息的字典列表。
    """
    # 添加文件路径None安全检查
    if not filepath:  # type: ignore
        logger.error(f"无效的文件路径: {filepath}")
        return []
    
    try:
        # 转换Path对象为字符串并检查是否存在
        file_path = str(Path(filepath))
        if not os.path.exists(file_path):
            logger.error(f"文件不存在: {file_path}")
            return []
        
        # 添加robust编码处理
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # 如果UTF-8失败，尝试其他编码
            try:
                with open(file_path, 'r', encoding='gbk') as f:
                    content = f.read()
                # 转换为UTF-8
                content = content.encode('gbk').decode('utf-8')
            except (UnicodeDecodeError, UnicodeError):
                # 最后尝试忽略错误
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
    except Exception as e:
        logger.error(f"无法读取文件: {filepath} - {e}")
        return []
    
    if not content:  # type: ignore
        logger.error("文件内容为空")
        return []

    # 检测是否为简化的键值对格式（重跑报告）
    if "失败论文重跑报告" in content and "---" in content:
        logger.info("检测到简化的键值对格式，执行解析...")
        return parse_simple_key_value_format(content)
    
    # 优先使用标准Zotero报告解析逻辑（更稳定）
    logger.info("使用标准Zotero报告格式解析...")
    standard_result = parse_standard_zotero_format(content)
    if standard_result:
        logger.info(f"标准格式解析成功，共解析 {len(standard_result)} 篇文献")
        return standard_result
    
    # 如果标准解析失败，尝试使用正则表达式增强解析
    logger.info("标准格式解析失败，尝试使用正则表达式增强解析...")
    regex_result = parse_with_regex(content)
    if regex_result:
        logger.info(f"正则表达式解析成功，共解析 {len(regex_result)} 篇文献")
        return regex_result
    
    # 所有方法都失败
    logger.error("所有解析方法都失败")
    return []


def parse_with_regex(content: str) -> List[PaperInfo]:
    """
    使用正则表达式解析Zotero报告（增强版）
    
    支持多种格式的Zotero报告，包括：
    - 标准Zotero导出格式
    - 各种变体格式
    - 包含特殊字符的格式
    - 自由文本格式的文献列表
    
    Args:
        content: 文件内容字符串
        
    Returns:
        解析后的文献列表，如果解析失败返回空列表
    """
    try:
        # 定义多种条目分隔模式
        entry_patterns = [
            # 模式1：标准Zotero格式（条目分隔符为"  *"）
            re.compile(r'^\s*\*\s*\n+(.*?)(?=^\s*\*\s*\n+|\Z)', re.MULTILINE | re.DOTALL),
            # 模式2：另一种常见格式（条目分隔符为"\n\n"且包含标题）
            re.compile(r'([^\n]+(?:\n\s+[^\n]+)*)\n\n', re.MULTILINE),
            # 模式3：包含"作者, 年份. 标题"格式的条目
            re.compile(r'([^\.\n]+(?:\s*,\s*[^\.\n]+)*\.\s*[^\.\n]+)', re.MULTILINE),
            # 模式4：Item Type开头的标准格式
            re.compile(r'(?:Item Type:.*?)(.*?)(?=Item Type:.*?|\Z)', re.MULTILINE | re.DOTALL | re.IGNORECASE),
            # 模式5：简单的空行分隔
            re.compile(r'([^\n]+(?:\n[^\n]+)*)\n\s*\n', re.MULTILINE)
        ]
        
        # 尝试各种模式匹配条目
        entries = []
        for pattern in entry_patterns:
            entries = pattern.findall(content)
            if entries:
                logger.info(f"使用模式匹配到 {len(entries)} 个条目")
                break
        
        if not entries:
            # 如果所有模式都失败，尝试按行分割并过滤
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            # 过滤掉明显不是文献条目的行（如标题、页眉等）
            entries = []
            current_entry = []
            
            for line in lines:
                # 如果这行看起来像新条目的开始
                if (re.match(r'^[A-Z]', line) and len(line) > 20 and 
                    not any(keyword in line.lower() for keyword in ['zotero', '报告', 'report', 'page', '页'])):
                    if current_entry:
                        entries.append('\n'.join(current_entry))  # type: ignore  # type: ignore  # type: ignore
                    current_entry = [line]
                else:
                    current_entry.append(line)  # type: ignore  # type: ignore  # type: ignore  # type: ignore
            
            if current_entry:
                entries.append('\n'.join(current_entry))  # type: ignore
        
        if not entries:
            logger.info("无法使用正则表达式匹配任何条目")
            return []
        
        parsed_entries: List[Dict[str, Any]] = []  # type: ignore
        
        # 增强的字段提取模式
        field_patterns = {
            'authors': [
                re.compile(r'作者[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Authors?[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s*,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)*)\s*,\s*\d{4}', re.MULTILINE),
                re.compile(r'^([^\d,]+(?:\s*,\s*[^\d,]+)*)\s*,\s*\d{4}', re.MULTILINE)
            ],
            'year': [
                re.compile(r'年份[:：]\s*(\d{4})(?:\n|$)', re.IGNORECASE),
                re.compile(r'Year[:：]\s*(\d{4})(?:\n|$)', re.IGNORECASE),
                re.compile(r'\((\d{4})\)(?:\n|$)'),
                re.compile(r',\s*(\d{4})[.,\s]'),
                re.compile(r'\b(19|20)\d{2}\b')
            ],
            'title': [
                re.compile(r'标题[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Title[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'^(.+?)(?:\n\s*作者[:：])', re.MULTILINE),
                re.compile(r'^([A-Z][^,.]+(?:\s+[A-Z][^,.]+)*)\s*,\s*\d{4}', re.MULTILINE)
            ],
            'journal': [
                re.compile(r'期刊[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Journal[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'\.\s*([^.,]+?),\s*\d'),
                re.compile(r'In\s+([^,\n]+),')
            ],
            'volume': [
                re.compile(r'卷次[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Volume[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'卷\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Vol\.\s*(.+?)(?:\n|,)', re.IGNORECASE),
                re.compile(r',\s*(\d+)\s*\('),  # 期刊名后接卷号，如 "Journal Name, 10(3)"
                re.compile(r'\.(\d+)\s*\(')   # 期刊名后接卷号，如 "Journal Name.10(3)"
            ],
            'issue': [
                re.compile(r'期号[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Issue[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'期\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'No\.\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'\((\d+)\)'),  # 卷号后的期号，如 "10(3)"
                re.compile(r',\s*(\d+)\s*\(')  # 直接期号，如 ", 3("
            ],
            'pages': [
                re.compile(r'页码[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Pages?[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'页\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'pp?\.\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'\)\s*:\s*(.+?)(?:\n|$)'),  # 期号后的页码，如 "(3): 123-145"
                re.compile(r'\)\s*(\d+-\d+)'),       # 期号后的页码，如 "(3)123-145"
                re.compile(r',\s*(\d+-\d+)')         # 直接页码范围
            ],
            'doi': [
                re.compile(r'DOI[:：]\s*(10\.\d+/.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'https?://doi\.org/(10\.\d+/.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'(10\.\d+/.+?)(?:\n|$)')
            ],
            'attachments': [
                re.compile(r'附件[:：]\s*(.+?\.pdf)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Attachment[:：]\s*(.+?\.pdf)(?:\n|$)', re.IGNORECASE),
                re.compile(r'([^\.\n]+\.pdf)(?:\n|$)', re.IGNORECASE)
            ]
        }
        
        for entry in entries:
            entry = entry.strip()
            if not entry or len(entry) < 20:  # 跳过空条目或太短的条目
                continue
            
            # 跳过明显的非文献条目（修复：只跳过整个条目是报告标题的情况）
            entry_lower = entry.lower().strip()
            if (entry_lower.startswith('zotero 报告') or 
                entry_lower.startswith('zotero report') or
                entry_lower == '报告' or 
                entry_lower == 'report'):
                continue
            
            paper: Dict[str, Any] = {  # type: ignore
                'authors': [],
                'attachments': []
            }
            
            # 尝试提取每个字段
            for field, patterns in field_patterns.items():
                for pattern in patterns:
                    match = pattern.search(entry)
                    if match:
                        group_1 = match.group(1) if match.groups() else None
                        if group_1 is None:
                            continue
                        value = group_1.strip()
                        
                        if not value:
                            continue
                        
                        if field == 'authors':
                            # 处理多种作者分隔符
                            authors = re.split(r'[,;，； and &]', value)
                            paper['authors'] = [a.strip() for a in authors if a.strip()]
                        elif field == 'year':
                            paper['year'] = value
                        elif field == 'attachments':
                            paper['attachments'] = [value]  # type: ignore
                        else:
                            paper[field] = value  # type: ignore
                        break  # 找到匹配后跳出内层循环
            
            # 如果没有找到标题，尝试智能提取
            if not paper.get('title'):  # type: ignore
                lines = entry.split('\n')
                for line in lines:
                    line = line.strip()
                    if (line and len(line) > 10 and 
                        not any(keyword in line for keyword in ['作者:', '年份:', '期刊:', 'DOI:', '附件:', 'Author:', 'Year:', 'Journal:']) and
                        not re.match(r'^\d+\.', line) and  # 跳过编号行
                        not line.startswith('条目类型')):  # 跳过Zotero字段标签
                        paper['title'] = line
                        break
            
            # 如果仍然没有找到标题，尝试从第一行提取
            if not paper.get('title') and entry:  # type: ignore
                entry_lines: List[str] = entry.split('\n') if entry else []  # type: ignore
                if entry_lines:
                    first_line: str = entry_lines[0].strip() if entry_lines[0] else ''  # type: ignore
                    if (first_line and len(str(first_line)) > 10 and 
                        not any(keyword in str(first_line).lower() for keyword in ['item type', 'zotero', '条目类型'])):
                        paper['title'] = first_line  # type: ignore
            
            # 只有包含标题的条目才被认为是有效的
            if paper.get('title'):  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore
                parsed_entries.append(paper)  # type: ignore  # type: ignore  # type: ignore  # type: ignore
        
        logger.info(f"正则表达式解析完成：共解析 {len(parsed_entries)} 篇文献")  # type: ignore
        return parsed_entries  # type: ignore
        
    except Exception as e:
        logger.error(f"正则表达式解析出错: {e}")
        return []


def parse_simple_key_value_format(content: str) -> List[PaperInfo]:
    """
    使用正则表达式增强的键值对格式解析
    
    格式示例：
    标题: 论文标题
    作者: 作者1, 作者2, 作者3
    年份: 2023
    期刊: 期刊名称
    DOI: 10.1234/example.doi
    附件: 文件名.pdf
    ---
    """
    # 添加content None安全检查
    if not content:  # type: ignore
        logger.error("无效的文件内容")
        return []

    parsed_entries = []
    
    # 使用正则表达式分割条目
    # 支持多种分隔符：--- 或 === 或 数字+.
    entry_pattern = re.compile(r'(?:(?:---|===)\s*)|(?:^\d+\.\s)', re.MULTILINE)
    entries = entry_pattern.split(content)
    
    for entry in entries:
        entry = entry.strip()
        if not entry or len(entry) < 10:  # 跳过空条目或太短的条目
            continue
            
        # 跳过头部信息（包含"失败论文重跑报告"的条目）
        if entry.startswith('失败论文重跑报告') or entry.startswith('Zotero 报告'):
            continue
            
        paper: Dict[str, Any] = {  # type: ignore
            'authors': [],
            'attachments': []
        }
        
        # 使用正则表达式提取键值对
        # 支持中英文冒号和多种键名
        patterns = {
            'title': [
                re.compile(r'标题[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Title[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'^(.+?)(?:\n\s*作者[:：])', re.IGNORECASE)  # 第一行作为标题，后面跟着作者
            ],
            'authors': [
                re.compile(r'作者[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Authors?[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE)
            ],
            'year': [
                re.compile(r'年份[:：]\s*(\d{4})(?:\n|$)', re.IGNORECASE),
                re.compile(r'Year[:：]\s*(\d{4})(?:\n|$)', re.IGNORECASE),
                re.compile(r'\((\d{4})\)(?:\n|$)')  # 括号中的年份
            ],
            'journal': [
                re.compile(r'期刊[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Journal[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'\.\s*([^.,]+?),\s*\d')  # 期刊名在句号后，逗号前
            ],
            'volume': [
                re.compile(r'卷次[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Volume[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'卷\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Vol\.\s*(.+?)(?:\n|,)', re.IGNORECASE)
            ],
            'issue': [
                re.compile(r'期号[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Issue[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'期\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'No\.\s*(.+?)(?:\n|$)', re.IGNORECASE)
            ],
            'pages': [
                re.compile(r'页码[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Pages?[:：]\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'页\s*(.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'pp?\.\s*(.+?)(?:\n|$)', re.IGNORECASE)
            ],
            'doi': [
                re.compile(r'DOI[:：]\s*(10\.\d+/.+?)(?:\n|$)', re.IGNORECASE),
                re.compile(r'https?://doi\.org/(10\.\d+/.+?)(?:\n|$)', re.IGNORECASE)
            ],
            'attachments': [
                re.compile(r'附件[:：]\s*(.+?\.pdf)(?:\n|$)', re.IGNORECASE),
                re.compile(r'Attachment[:：]\s*(.+?\.pdf)(?:\n|$)', re.IGNORECASE),
                re.compile(r'([^\.\n]+\.pdf)(?:\n|$)', re.IGNORECASE)  # 任何以.pdf结尾的文件名
            ]
        }
        
        # 尝试提取每个字段
        for field, field_patterns in patterns.items():
            for pattern in field_patterns:
                match = pattern.search(entry)
                if match:
                    group_1 = match.group(1) if match.groups() else None
                    if group_1 is None:
                        continue
                    value = group_1.strip()
                    
                    if not value:
                        continue
                    
                    if field == 'authors':
                        # 处理多种作者分隔符
                        authors = re.split(r'[,;，； and &]', value)
                        paper['authors'] = [a.strip() for a in authors if a.strip()]  # type: ignore
                    elif field == 'year':
                        paper['year'] = value
                    elif field == 'attachments':
                        paper['attachments'] = [value]  # type: ignore
                    else:
                        paper[field] = value  # type: ignore
                    break  # 找到匹配后跳出内层循环
        
        # 只有包含标题的条目才被认为是有效的
        if paper.get('title'):  # type: ignore
            parsed_entries.append(paper)  # type: ignore
    
    logger.info(f"简化格式解析完成：共解析 {len(parsed_entries)} 篇文献")  # type: ignore
    return parsed_entries  # type: ignore


def parse_standard_zotero_format(content: str) -> List[PaperInfo]:
    """
    使用正则表达式增强的标准Zotero报告格式解析。
    本版本修复了所有已知的语法错误和逻辑问题。
    """
    # 添加content None安全检查
    if not content:  # type: ignore
        logger.error("无效的文件内容")
        return []

    # +++ 最终修复代码 +++
    # 使用更可靠的方法分割条目，正确处理Zotero报告的实际格式
    # Zotero报告格式：每篇文献以单独一行的"*"开头，然后是内容
    
    # 方法1：先尝试使用正则表达式（修复：处理*后的多个空行）
    entry_pattern = re.compile(r'^\s*\*\s*\n+(.*?)(?=^\s*\*\s*\n+|\Z)', re.MULTILINE | re.DOTALL)
    entries_text = entry_pattern.findall(content)
    
    # 方法2：如果正则表达式失败，使用手动分割方法（增强版）
    if len(entries_text) <= 1:
        lines = content.split('\n')
        entries = []
        current_entry = []
        
        for line in lines:
            if line.strip() == '*':  # type: ignore
                if current_entry:
                    entries.append('\n'.join(current_entry))  # type: ignore
                current_entry = []
            else:  # type: ignore
                current_entry.append(line)  # type: ignore
        
        if current_entry:
            entries.append('\n'.join(current_entry))  # type: ignore
        
        entries_text = entries
    
    # 方法3：如果仍然失败，回退到原始方法
    if len(entries_text) <= 1:
        entries_text = content.split('  *')

    parsed_entries: List[Dict[str, Any]] = []  # type: ignore

    # 字段映射
    key_mapping = {
        '条目类型': 'item_type', '摘要': 'abstract', '语言': 'language',
        '文库编目': 'library_catalog', '其他': 'other', '添加日期': 'date_added',
        '修改日期': 'date_modified', '日期': 'date', '短标题': 'short_title',
        '网址': 'url', '访问时间': 'access_date', '版权': 'rights',
        '卷次': 'volume', '页码': 'pages', '刊名': 'publication_title',
        'DOI': 'doi', '期号': 'issue', 'ISSN': 'issn'
    }

    # 定义所有正则表达式模式 (使用三引号确保多行安全)
    tab_kv_pattern = re.compile(r"""^([^\t]+)\t(.+)""", re.MULTILINE)
    tag_start_pattern = re.compile(r"""^          标签[:：]""", re.MULTILINE)
    attachment_start_pattern = re.compile(r"""^          附件""", re.MULTILINE)
    tag_item_pattern = re.compile(r"""^\s*o\s+(.+)""")

    for entry_text in entries_text:
        if len(entry_text.strip()) < 20:
            continue

        paper: Dict[str, Any] = {  # type: ignore
            'authors': [], 'editors': [], 'tags': [], 'attachments': []
        }

        lines = entry_text.strip().split('\n')
        title_found = False
        in_tags_section = False
        in_attachments_section = False
        current_attachment = ""

        for line in lines:
            if line is None:
                continue
            line = line.rstrip()
            if not line:
                continue

            if tag_start_pattern.search(line):
                in_tags_section = True
                in_attachments_section = False
                if current_attachment.strip():
                    paper['attachments'].append(current_attachment.strip())  # type: ignore
                    current_attachment = ""
                continue

            if attachment_start_pattern.search(line) or (line.strip().endswith('附件') and '\t' not in line):
                if current_attachment.strip():
                    paper['attachments'].append(current_attachment.strip())  # type: ignore
                    current_attachment = ""
                in_attachments_section = True
                in_tags_section = False
                continue

            if in_tags_section and not in_attachments_section:
                tag_match = tag_item_pattern.match(line)
                if tag_match:
                    group_1 = tag_match.group(1) if tag_match.groups() else None
                    if group_1 and isinstance(group_1, str):
                        paper['tags'].append(group_1.strip())  # type: ignore
                continue

            if in_attachments_section:
                stripped_line = line.lstrip()
                tag_match = tag_item_pattern.match(stripped_line)
                if tag_match:
                    if current_attachment.strip():
                        paper['attachments'].append(current_attachment.strip())  # type: ignore
                    group_1 = tag_match.group(1) if tag_match.groups() else None
                    if group_1 and isinstance(group_1, str):
                        current_attachment = group_1.strip()
                elif line.strip():
                    if current_attachment:
                        current_attachment += ' ' + line.strip()
                    else:
                        current_attachment = line.strip()
                continue

            kv_match = tab_kv_pattern.match(line)
            if kv_match:
                groups = kv_match.groups()
                if len(groups) >= 2 and isinstance(groups[0], str) and isinstance(groups[1], str):
                    key, value = groups[0].strip(), groups[1].strip()
                else:
                    continue
                if key == '作者':
                    paper['authors'].append(value)  # type: ignore
                elif key == '编辑':
                    paper['editors'].append(value)  # type: ignore
                elif key in key_mapping:
                    paper[key_mapping[key]] = value
                else:
                    paper[key] = value
                continue

            exclude_titles = ['zotero 报告', 'zotero report', '报告', 'report']
            line_lower = line.lower().strip()
            if not title_found and not in_tags_section and not in_attachments_section and line_lower not in exclude_titles:
                if line is not None:
                    paper['title'] = line.strip()
                    title_found = True

        if current_attachment and current_attachment.strip():
            paper['attachments'].append(current_attachment.strip())  # type: ignore

        if paper.get('title'):  # type: ignore
            parsed_entries.append(paper)  # type: ignore

    logger.info(f"标准Zotero报告解析完成：共解析 {len(parsed_entries)} 篇文献")  # type: ignore
    return parsed_entries  # type: ignore


if __name__ == "__main__":
    # 测试函数
    import sys
    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        papers = parse_zotero_report(test_file)
        logger.info(f"解析完成，共找到 {len(papers)} 篇文献")

        for i, paper in enumerate(papers[:3]):
            logger.info(f"\n文献 {i+1}:")
            logger.info(f"  标题: {paper.get('title', '未知')}")  # type: ignore
            logger.info(f"  附件数: {len(paper.get('attachments', []))}")  # type: ignore
            for j, attachment in enumerate(paper.get('attachments', [])):  # type: ignore
                logger.info(f"    附件{j+1}: {attachment}")
    else:
        logger.info("使用方法: python zotero_parser.py <zotero报告文件路径>")