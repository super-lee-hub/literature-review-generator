#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manual report-data extraction checker for canonical summaries."""

from __future__ import annotations

import json
import os

from summary_schema import get_ai_summary, get_core_analysis


def check_data_extraction_fixed() -> bool:
    """Check whether report extraction reads canonical summaries correctly."""
    try:
        json_file = "output/案例分析/案例分析_summaries.json"
        if not os.path.exists(json_file):
            print(f"未找到 JSON 文件: {json_file}")
            return False

        with open(json_file, "r", encoding="utf-8") as handle:
            summaries = json.load(handle)

        print(f"成功加载 JSON 文件，共 {len(summaries)} 篇论文")
        print("=" * 80)

        for index, summary in enumerate(summaries[:3]):
            print(f"\n测试论文 #{index + 1}:")
            paper_info = summary.get("paper_info", {})
            ai_summary = get_ai_summary(summary)
            core_analysis = get_core_analysis(ai_summary)

            print("  数据源检查:")
            print(f"    - paper_info: {'✅' if bool(paper_info) else '❌'}")
            print(f"    - ai_summary: {'✅' if bool(summary.get('ai_summary')) else '❌'}")
            print(f"    - canonical.core_analysis: {'✅' if bool(core_analysis) else '❌'}")
            print(f"    - schema_version: {ai_summary.get('schema_version', '')}")

            title = paper_info.get("title", "")
            authors = ", ".join(paper_info.get("authors", [])) if paper_info.get("authors") else ""
            year = paper_info.get("year", "")
            journal = paper_info.get("journal", "")

            summary_text = core_analysis.get("summary") or ""
            methodology = core_analysis.get("methodology") or ""
            findings = core_analysis.get("findings") or ""
            conclusions = core_analysis.get("conclusions") or ""

            print("  提取结果:")
            print(f"    标题: {title[:50]}{'...' if len(title) > 50 else ''}")
            print(f"    作者: {authors}")
            print(f"    年份: {year}")
            print(f"    期刊: {journal}")
            print(f"    摘要长度: {len(summary_text)} 字符")
            print(f"    方法长度: {len(methodology)} 字符")
            print(f"    发现长度: {len(findings)} 字符")
            print(f"    结论长度: {len(conclusions)} 字符")

            basic_fields = [title, authors, year, journal]
            analysis_fields = [summary_text, methodology, findings, conclusions]
            basic_empty = sum(1 for field in basic_fields if not str(field).strip())
            analysis_empty = sum(1 for field in analysis_fields if not str(field).strip())

            print("  数据完整性:")
            print(f"    基本信息: {4 - basic_empty}/4 字段有数据")
            print(f"    分析内容: {4 - analysis_empty}/4 字段有数据")

        print("\n" + "=" * 80)
        print("数据提取测试完成")
        return True
    except Exception as exc:
        print(f"测试过程中出错: {exc}")
        import traceback

        traceback.print_exc()
        return False


def main() -> None:
    print("开始 Excel 数据提取逻辑测试（canonical-first）...")
    success = check_data_extraction_fixed()
    if success:
        print("\n测试完成。如果主要字段都有数据，说明 canonical 读取路径正常。")
    else:
        print("\n测试失败，需要进一步检查。")


if __name__ == "__main__":
    main()
