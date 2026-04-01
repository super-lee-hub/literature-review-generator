#!/usr/bin/env python3
"""Quick placeholder diagnostics for generated summary files."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Mapping

from summary_schema import get_core_analysis


PLACEHOLDER_KEYWORDS = [
    "未提供相关信息",
    "未提及",
    "未提供",
    "无相关信息",
    "未知",
    "Not provided",
    "N/A",
    "null",
    "None",
    "...",
    "无摘要",
    "无数据",
]


def _contains_placeholder(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    text = str(value)
    return any(keyword in text for keyword in PLACEHOLDER_KEYWORDS)


def quick_placeholder_check(file_path: str) -> Dict[str, Any]:
    """Quickly inspect one summaries JSON file for placeholder content."""
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        return {"error": f"无法读取文件: {exc}"}

    result: Dict[str, Any] = {
        "file_path": file_path,
        "total_papers": len(data),
        "placeholder_papers": 0,
        "placeholder_examples": [],
    }

    for index, paper in enumerate(data):
        paper_info = paper.get("paper_info", {}) if isinstance(paper, Mapping) else {}
        core = get_core_analysis(paper)

        placeholder_fields: List[str] = []
        for field in ["title", "year", "authors", "journal"]:
            value = paper_info.get(field, "")
            if _contains_placeholder(value):
                placeholder_fields.append(f"{field}: {value}")

        for field in ["summary", "findings", "methodology", "conclusions", "relevance", "limitations"]:
            value = core.get(field)
            if _contains_placeholder(value):
                preview = str(value)[:50]
                placeholder_fields.append(f"{field}: {preview}...")

        for key_point in core.get("key_points", []):
            if _contains_placeholder(key_point):
                placeholder_fields.append(f"key_points: {key_point}")

        if placeholder_fields:
            result["placeholder_papers"] += 1
            result["placeholder_examples"].append(
                {
                    "index": index,
                    "title": paper_info.get("title", "未知标题"),
                    "fields": placeholder_fields[:3],
                }
            )

    return result


def main() -> None:
    """Run placeholder diagnostics across the output directory."""
    print("开始快速占位符检查...")

    summaries_files: List[str] = []
    for root, _, files in os.walk("output"):
        for file_name in files:
            if file_name.endswith("_summaries.json"):
                summaries_files.append(os.path.join(root, file_name))

    if not summaries_files:
        print("未找到任何 summaries.json 文件")
        return

    print(f"找到 {len(summaries_files)} 个 JSON 文件")
    total_papers = 0
    total_placeholders = 0

    for file_path in summaries_files:
        print(f"\n检查: {file_path}")
        result = quick_placeholder_check(file_path)
        if "error" in result:
            print(result["error"])
            continue

        total_papers += int(result["total_papers"])
        total_placeholders += int(result["placeholder_papers"])
        placeholder_rate = (
            result["placeholder_papers"] / result["total_papers"] * 100 if result["total_papers"] else 0
        )

        print(f"  总论文数: {result['total_papers']}")
        print(f"  占位符论文数: {result['placeholder_papers']}")
        print(f"  占位符比例: {placeholder_rate:.1f}%")

        for example in result["placeholder_examples"][:2]:
            print(f"  示例 - 论文{example['index'] + 1}: {example['title']}")
            for field in example["fields"][:2]:
                print(f"    * {field}")

    print("\n总体统计")
    print(f"总论文数: {total_papers}")
    print(f"占位符论文数: {total_placeholders}")
    if total_papers:
        overall_rate = total_placeholders / total_papers * 100
        print(f"总体占位符比例: {overall_rate:.1f}%")


if __name__ == "__main__":
    main()
