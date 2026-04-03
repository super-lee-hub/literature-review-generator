"""
Zotero 报告解析模块。
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, cast

from models import PaperInfo  # type: ignore
from services.text_io import read_text_file_with_fallbacks


logger = logging.getLogger(__name__)


def _split_authors(value: str) -> List[str]:
    """Split author strings without breaking `Last, First` names."""

    normalized = str(value or "").strip()
    if not normalized:
        return []

    primary_parts = [
        item.strip()
        for item in re.split(r"\s*(?:;|；|、|\band\b|&)\s*", normalized, flags=re.IGNORECASE)
        if item.strip()
    ]
    if len(primary_parts) > 1:
        return primary_parts

    comma_parts = [item.strip() for item in re.split(r"\s*,\s*", normalized) if item.strip()]
    if len(comma_parts) <= 1:
        return comma_parts

    if len(comma_parts) % 2 == 0:
        paired_names = [
            f"{comma_parts[index]}, {comma_parts[index + 1]}"
            for index in range(0, len(comma_parts), 2)
        ]
        if all(name.strip() for name in paired_names):
            return paired_names

    return comma_parts


def parse_zotero_report(filepath: str) -> List[PaperInfo]:
    """增强的 Zotero 报告解析函数，支持标准格式、键值对格式和正则兜底。"""

    if not filepath:  # type: ignore
        logger.error(f"无效的文件路径: {filepath}")
        return []

    try:
        file_path = str(Path(filepath))
        if not os.path.exists(file_path):
            logger.error(f"文件不存在: {file_path}")
            return []

        content = read_text_file_with_fallbacks(file_path, logger=logger)
    except Exception as exc:
        logger.error(f"无法读取文件: {filepath} - {exc}")
        return []

    if not content:  # type: ignore
        logger.error("文件内容为空")
        return []

    if "失败论文重跑报告" in content and "---" in content:
        logger.info("检测到简化的键值对格式，执行解析...")
        return parse_simple_key_value_format(content)

    logger.info("使用标准 Zotero 报告格式解析...")
    standard_result = parse_standard_zotero_format(content)
    if standard_result:
        logger.info(f"标准格式解析成功，共解析 {len(standard_result)} 篇文献")
        return standard_result

    logger.info("标准格式解析失败，尝试使用正则表达式增强解析...")
    regex_result = parse_with_regex(content)
    if regex_result:
        logger.info(f"正则表达式解析成功，共解析 {len(regex_result)} 篇文献")
        return regex_result

    logger.error("所有解析方法都失败")
    return []


def parse_with_regex(content: str) -> List[PaperInfo]:
    """使用正则表达式解析 Zotero 报告（增强版）。"""

    try:
        entry_patterns = [
            re.compile(r"^\s*\*\s*\n+(.*?)(?=^\s*\*\s*\n+|\Z)", re.MULTILINE | re.DOTALL),
            re.compile(r"([^\n]+(?:\n\s+[^\n]+)*)\n\n", re.MULTILINE),
            re.compile(r"([^\.\n]+(?:\s*,\s*[^\.\n]+)*\.\s*[^\.\n]+)", re.MULTILINE),
            re.compile(r"(?:Item Type:.*?)(.*?)(?=Item Type:.*?|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE),
            re.compile(r"([^\n]+(?:\n[^\n]+)*)\n\s*\n", re.MULTILINE),
        ]

        entries: List[str] = []
        for pattern in entry_patterns:
            entries = pattern.findall(content)
            if entries:
                logger.info(f"使用模式匹配到 {len(entries)} 个条目")
                break

        if not entries:
            lines = [line.strip() for line in content.split("\n") if line.strip()]
            entries = []
            current_entry: List[str] = []

            for line in lines:
                if re.match(r"^[A-Z]", line) and len(line) > 20 and not any(
                    keyword in line.lower() for keyword in ["zotero", "报告", "report", "page", "页"]
                ):
                    if current_entry:
                        entries.append("\n".join(current_entry))
                    current_entry = [line]
                else:
                    current_entry.append(line)

            if current_entry:
                entries.append("\n".join(current_entry))

        if not entries:
            logger.info("无法使用正则表达式匹配任何条目")
            return []

        parsed_entries: List[PaperInfo] = []
        field_patterns = {
            "authors": [
                re.compile(r"作者[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Authors?[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(
                    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s*,\s*[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)*)\s*,\s*\d{4}",
                    re.MULTILINE,
                ),
                re.compile(r"^([^\d,]+(?:\s*,\s*[^\d,]+)*)\s*,\s*\d{4}", re.MULTILINE),
            ],
            "year": [
                re.compile(r"年份[:：]\s*(\d{4})(?:\n|$)", re.IGNORECASE),
                re.compile(r"Year[:：]\s*(\d{4})(?:\n|$)", re.IGNORECASE),
                re.compile(r"\((\d{4})\)(?:\n|$)"),
                re.compile(r",\s*(\d{4})[.,\s]"),
                re.compile(r"\b(?:19|20)\d{2}\b"),
            ],
            "title": [
                re.compile(r"标题[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Title[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"^(.+?)(?:\n\s*作者[:：])", re.MULTILINE),
                re.compile(r"^([A-Z][^,.]+(?:\s+[A-Z][^,.]+)*)\s*,\s*\d{4}", re.MULTILINE),
            ],
            "journal": [
                re.compile(r"期刊[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Journal[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"\.\s*([^.,]+?),\s*\d"),
                re.compile(r"In\s+([^,\n]+),"),
            ],
            "volume": [
                re.compile(r"卷次[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Volume[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"卷\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Vol\.\s*(.+?)(?:\n|,)", re.IGNORECASE),
                re.compile(r",\s*(\d+)\s*\("),
                re.compile(r"\.(\d+)\s*\("),
            ],
            "issue": [
                re.compile(r"期号[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Issue[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"^\s*期(?!刊)\s*[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE | re.MULTILINE),
                re.compile(r"No\.\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"\((\d+)\)"),
                re.compile(r",\s*(\d+)\s*\("),
            ],
            "pages": [
                re.compile(r"页码[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Pages?[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"页\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"pp?\.\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"\)\s*:\s*(.+?)(?:\n|$)"),
                re.compile(r"\)\s*(\d+-\d+)"),
                re.compile(r",\s*(\d+-\d+)"),
            ],
            "doi": [
                re.compile(r"DOI[:：]\s*(10\.\d+/.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"https?://doi\.org/(10\.\d+/.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"(10\.\d+/.+?)(?:\n|$)"),
            ],
            "attachments": [
                re.compile(r"附件[:：]\s*(.+?\.pdf)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Attachment[:：]\s*(.+?\.pdf)(?:\n|$)", re.IGNORECASE),
                re.compile(r"([^\.\n]+\.pdf)(?:\n|$)", re.IGNORECASE),
            ],
        }

        for entry in entries:
            entry = entry.strip()
            if not entry or len(entry) < 20:
                continue

            entry_lower = entry.lower().strip()
            if (
                entry_lower.startswith("zotero 报告")
                or entry_lower.startswith("zotero report")
                or entry_lower == "报告"
                or entry_lower == "report"
            ):
                continue

            paper: Dict[str, Any] = {"authors": [], "attachments": []}

            for field, patterns in field_patterns.items():
                for pattern in patterns:
                    match = pattern.search(entry)
                    if not match:
                        continue
                    value = (match.group(1) if match.groups() else "").strip()
                    if not value:
                        continue

                    if field == "authors":
                        paper["authors"] = _split_authors(value)
                    elif field == "year":
                        paper["year"] = value
                    elif field == "attachments":
                        paper["attachments"] = [value]
                    else:
                        paper[field] = value
                    break

            if not paper.get("title"):
                lines = entry.split("\n")
                for line in lines:
                    line = line.strip()
                    if (
                        line
                        and len(line) > 10
                        and not any(
                            keyword in line
                            for keyword in [
                                "作者:",
                                "年份:",
                                "期刊:",
                                "DOI:",
                                "附件:",
                                "Author:",
                                "Year:",
                                "Journal:",
                            ]
                        )
                        and not re.match(r"^\d+\.", line)
                        and not line.startswith("条目类型")
                    ):
                        paper["title"] = line
                        break

            if not paper.get("title") and entry:
                entry_lines = entry.split("\n")
                if entry_lines:
                    first_line = entry_lines[0].strip()
                    if first_line and len(first_line) > 10 and not any(
                        keyword in first_line.lower() for keyword in ["item type", "zotero", "条目类型"]
                    ):
                        paper["title"] = first_line

            if paper.get("title"):
                parsed_entries.append(cast(PaperInfo, paper))

        logger.info(f"正则表达式解析完成：共解析 {len(parsed_entries)} 篇文献")
        return parsed_entries

    except Exception as exc:
        logger.error(f"正则表达式解析出错: {exc}")
        return []


def parse_simple_key_value_format(content: str) -> List[PaperInfo]:
    """使用键值对规则解析失败论文重跑报告。"""

    if not content:  # type: ignore
        logger.error("无效的文件内容")
        return []

    parsed_entries: List[PaperInfo] = []
    entry_pattern = re.compile(r"(?:(?:---|===)\s*)|(?:^\d+\.\s)", re.MULTILINE)
    entries = entry_pattern.split(content)

    for entry in entries:
        entry = entry.strip()
        if not entry or len(entry) < 10:
            continue

        if entry.startswith("失败论文重跑报告") or entry.startswith("Zotero 报告"):
            continue

        paper: Dict[str, Any] = {"authors": [], "attachments": []}
        patterns = {
            "title": [
                re.compile(r"标题[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Title[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"^(.+?)(?:\n\s*作者[:：])", re.IGNORECASE),
            ],
            "authors": [
                re.compile(r"作者[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Authors?[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
            ],
            "year": [
                re.compile(r"年份[:：]\s*(\d{4})(?:\n|$)", re.IGNORECASE),
                re.compile(r"Year[:：]\s*(\d{4})(?:\n|$)", re.IGNORECASE),
                re.compile(r"\((\d{4})\)(?:\n|$)"),
            ],
            "journal": [
                re.compile(r"期刊[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Journal[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"\.\s*([^.,]+?),\s*\d"),
            ],
            "volume": [
                re.compile(r"卷次[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Volume[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"卷\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Vol\.\s*(.+?)(?:\n|,)", re.IGNORECASE),
            ],
            "issue": [
                re.compile(r"期号[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Issue[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"^\s*期(?!刊)\s*[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE | re.MULTILINE),
                re.compile(r"No\.\s*(.+?)(?:\n|$)", re.IGNORECASE),
            ],
            "pages": [
                re.compile(r"页码[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Pages?[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"页\s*(.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"pp?\.\s*(.+?)(?:\n|$)", re.IGNORECASE),
            ],
            "doi": [
                re.compile(r"DOI[:：]\s*(10\.\d+/.+?)(?:\n|$)", re.IGNORECASE),
                re.compile(r"https?://doi\.org/(10\.\d+/.+?)(?:\n|$)", re.IGNORECASE),
            ],
            "attachments": [
                re.compile(r"附件[:：]\s*(.+?\.pdf)(?:\n|$)", re.IGNORECASE),
                re.compile(r"Attachment[:：]\s*(.+?\.pdf)(?:\n|$)", re.IGNORECASE),
                re.compile(r"([^\.\n]+\.pdf)(?:\n|$)", re.IGNORECASE),
            ],
        }

        for field, field_patterns in patterns.items():
            for pattern in field_patterns:
                match = pattern.search(entry)
                if not match:
                    continue
                value = (match.group(1) if match.groups() else "").strip()
                if not value:
                    continue

                if field == "authors":
                    paper["authors"] = _split_authors(value)
                elif field == "year":
                    paper["year"] = value
                elif field == "attachments":
                    paper["attachments"] = [value]
                else:
                    paper[field] = value
                break

        if paper.get("title"):
            parsed_entries.append(cast(PaperInfo, paper))

    logger.info(f"简化格式解析完成：共解析 {len(parsed_entries)} 篇文献")
    return parsed_entries


def parse_standard_zotero_format(content: str) -> List[PaperInfo]:
    """使用增强规则解析标准 Zotero 报告格式。"""

    if not content:  # type: ignore
        logger.error("无效的文件内容")
        return []

    entry_pattern = re.compile(r"^\s*\*\s*\n+(.*?)(?=^\s*\*\s*\n+|\Z)", re.MULTILINE | re.DOTALL)
    entries_text = entry_pattern.findall(content)

    if len(entries_text) <= 1:
        lines = content.split("\n")
        entries: List[str] = []
        current_entry: List[str] = []

        for line in lines:
            if line.strip() == "*":  # type: ignore
                if current_entry:
                    entries.append("\n".join(current_entry))
                current_entry = []
            else:
                current_entry.append(line)

        if current_entry:
            entries.append("\n".join(current_entry))

        entries_text = entries

    if len(entries_text) <= 1:
        entries_text = content.split("  *")

    parsed_entries: List[PaperInfo] = []
    key_mapping = {
        "条目类型": "item_type",
        "摘要": "abstract",
        "语言": "language",
        "文库编目": "library_catalog",
        "其他": "other",
        "添加日期": "date_added",
        "修改日期": "date_modified",
        "日期": "date",
        "短标题": "short_title",
        "网址": "url",
        "访问时间": "access_date",
        "版权": "rights",
        "卷次": "volume",
        "页码": "pages",
        "刊名": "publication_title",
        "DOI": "doi",
        "期号": "issue",
        "ISSN": "issn",
    }

    tab_kv_pattern = re.compile(r"^([^\t]+)\t(.+)", re.MULTILINE)
    tag_start_pattern = re.compile(r"^          标签[:：]", re.MULTILINE)
    attachment_start_pattern = re.compile(r"^          附件", re.MULTILINE)
    tag_item_pattern = re.compile(r"^\s*o\s+(.+)")

    for entry_text in entries_text:
        if len(entry_text.strip()) < 20:
            continue

        paper: Dict[str, Any] = {"authors": [], "editors": [], "tags": [], "attachments": []}
        lines = entry_text.strip().split("\n")
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
                    paper["attachments"].append(current_attachment.strip())
                    current_attachment = ""
                continue

            if attachment_start_pattern.search(line) or (line.strip().endswith("附件") and "\t" not in line):
                if current_attachment.strip():
                    paper["attachments"].append(current_attachment.strip())
                    current_attachment = ""
                in_attachments_section = True
                in_tags_section = False
                continue

            if in_tags_section and not in_attachments_section:
                tag_match = tag_item_pattern.match(line)
                if tag_match and tag_match.group(1):
                    paper["tags"].append(tag_match.group(1).strip())
                continue

            if in_attachments_section:
                stripped_line = line.lstrip()
                tag_match = tag_item_pattern.match(stripped_line)
                if tag_match:
                    if current_attachment.strip():
                        paper["attachments"].append(current_attachment.strip())
                    if tag_match.group(1):
                        current_attachment = tag_match.group(1).strip()
                elif line.strip():
                    if current_attachment:
                        current_attachment += " " + line.strip()
                    else:
                        current_attachment = line.strip()
                continue

            kv_match = tab_kv_pattern.match(line)
            if kv_match:
                key, value = kv_match.group(1).strip(), kv_match.group(2).strip()
                if key == "作者":
                    paper["authors"].append(value)
                elif key == "编辑":
                    paper["editors"].append(value)
                elif key in key_mapping:
                    paper[key_mapping[key]] = value
                else:
                    paper[key] = value
                continue

            exclude_titles = ["zotero 报告", "zotero report", "报告", "report"]
            line_lower = line.lower().strip()
            if not title_found and not in_tags_section and not in_attachments_section and line_lower not in exclude_titles:
                paper["title"] = line.strip()
                title_found = True

        if current_attachment and current_attachment.strip():
            paper["attachments"].append(current_attachment.strip())

        if paper.get("title"):
            parsed_entries.append(cast(PaperInfo, paper))

    logger.info(f"标准 Zotero 报告解析完成：共解析 {len(parsed_entries)} 篇文献")
    return parsed_entries


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        papers = parse_zotero_report(test_file)
        logger.info(f"解析完成，共找到 {len(papers)} 篇文献")

        for index, paper in enumerate(papers[:3], 1):
            logger.info(f"\n文献 {index}:")
            logger.info(f"  标题: {paper.get('title', '未知')}")  # type: ignore
            logger.info(f"  附件数: {len(paper.get('attachments', []))}")  # type: ignore
            for attachment_index, attachment in enumerate(paper.get("attachments", []), 1):  # type: ignore
                logger.info(f"    附件{attachment_index}: {attachment}")
    else:
        logger.info("使用方法: python zotero_parser.py <zotero报告文件路径>")
