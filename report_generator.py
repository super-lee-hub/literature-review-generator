"""
Report generation helpers.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Sequence, Tuple, cast

import pandas as pd  # type: ignore

from services.text_io import load_json_file_with_fallbacks
from summary_schema import (
    canonical_ai_summary_json,
    get_ai_summary,
    get_core_analysis,
    get_primary_sheet_target,
    get_quality_audit,
    get_routing,
    get_specialized_details,
)


BASE_COLUMNS = ["论文标题", "作者", "发表年份", "期刊名称", "DOI"]
COMMON_ANALYSIS_COLUMNS = ["研究摘要", "关键要点", "研究方法", "主要发现", "研究结论", "理论贡献", "研究局限"]
ROUTING_AUDIT_COLUMNS = [
    "论文类型",
    "子类型标准值",
    "分类状态",
    "路由置信度",
    "提取置信度",
    "完整度",
    "是否建议人工复核",
    "分类依据",
    "primary_sheet_target",
    "缺失关键字段数",
    "冲突标记",
]
RUNTIME_COLUMNS = ["解析器", "文本长度", "处理状态", "处理时间"]

TOTAL_COLUMNS = BASE_COLUMNS + COMMON_ANALYSIS_COLUMNS + ROUTING_AUDIT_COLUMNS + [
    "理论框架",
    "研究空白",
    "未来研究方向",
] + RUNTIME_COLUMNS + ["detailed_json"]

EMPIRICAL_COLUMNS = BASE_COLUMNS + COMMON_ANALYSIS_COLUMNS + ROUTING_AUDIT_COLUMNS + [
    "理论框架",
    "研究空白",
    "研究问题/假设",
    "数据来源与样本",
    "分析技术",
    "核心变量",
    "样本/情境",
    "未来研究方向",
] + RUNTIME_COLUMNS + ["detailed_json"]

REVIEW_COLUMNS = BASE_COLUMNS + COMMON_ANALYSIS_COLUMNS + ROUTING_AUDIT_COLUMNS + [
    "理论框架",
    "研究空白",
    "综述类型",
    "检索数据库",
    "时间范围",
    "纳入文献数量",
    "纳排标准",
    "综合方法",
    "主题聚类",
    "未来研究方向",
] + RUNTIME_COLUMNS + ["detailed_json"]

CONCEPTUAL_COLUMNS = BASE_COLUMNS + COMMON_ANALYSIS_COLUMNS + ROUTING_AUDIT_COLUMNS + [
    "理论框架",
    "研究空白",
    "核心命题",
    "概念关系",
    "理论贡献（类型详情）",
    "未来研究方向",
] + RUNTIME_COLUMNS + ["detailed_json"]

UNCERTAIN_COLUMNS = BASE_COLUMNS + COMMON_ANALYSIS_COLUMNS + ROUTING_AUDIT_COLUMNS + [
    "理论框架",
    "研究空白",
    "未来研究方向",
] + RUNTIME_COLUMNS + ["detailed_json"]


def read_json_robust(file_path: str) -> Any:
    try:
        return load_json_file_with_fallbacks(file_path)
    except Exception:
        return []


def _stringify_list(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value).strip()


_ILLEGAL_EXCEL_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def _sanitize_excel_string(value: str) -> str:
    """Remove characters that Excel/openpyxl cannot serialize into worksheets."""

    cleaned = _ILLEGAL_EXCEL_CONTROL_CHARS_RE.sub("", value)
    if not cleaned:
        return cleaned

    filtered_chars: List[str] = []
    for char in cleaned:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            continue
        if codepoint in {0xFFFE, 0xFFFF}:
            continue
        filtered_chars.append(char)
    return "".join(filtered_chars)


def _sanitize_excel_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_excel_string(value)
    return value


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_content(item) for item in value)
    if isinstance(value, Mapping):
        return any(_has_content(item) for item in value.values())
    return True


def _get_summary_sections(summary: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    paper_info = summary.get("paper_info", {}) if isinstance(summary, dict) else {}
    preprocess = summary.get("preprocess", {}) if isinstance(summary, dict) else {}
    ai_summary = get_ai_summary(summary)
    return paper_info, get_core_analysis(ai_summary), get_routing(ai_summary), get_specialized_details(ai_summary), preprocess


def _canonical_paper_type(summary_like: Any) -> str:
    return str(get_routing(summary_like).get("paper_type") or "")


def _flatten_type_specific_details(summary_like: Any) -> Dict[str, str]:
    routing = get_routing(summary_like)
    core = get_core_analysis(summary_like)
    specialized = get_specialized_details(summary_like)
    quality = get_quality_audit(summary_like)
    empirical = specialized.get("empirical") or {}

    return {
        "论文类型": str(routing.get("paper_type") or ""),
        "论文子类型": str(routing.get("paper_subtype_normalized") or ""),
        "分类状态": str(routing.get("classification_status") or ""),
        "路由置信度": str(routing.get("route_confidence") or ""),
        "分类依据": str(routing.get("classification_rationale") or ""),
        "理论框架": str(core.get("theoretical_framework") or ""),
        "研究空白": str(core.get("research_gap") or ""),
        "研究问题/假设": _stringify_list(empirical.get("research_questions_or_hypotheses", [])),
        "数据来源与样本": str(empirical.get("data_source_and_size") or ""),
        "分析技术": str(empirical.get("analysis_technique") or ""),
        "样本/情境": str(empirical.get("sample_characteristics_or_context") or ""),
        "未来研究方向": _stringify_list(core.get("future_research_directions", [])),
        "提取置信度": str(quality.get("extraction_confidence") or ""),
    }


def _format_core_variables(core_variables: Any) -> str:
    if not isinstance(core_variables, Mapping):
        return ""
    labels = {
        "independent": "自变量",
        "dependent": "因变量",
        "mediators": "中介变量",
        "moderators": "调节变量",
        "controls": "控制变量",
        "other_core_constructs": "其他核心构念",
    }
    parts: List[str] = []
    for key, label in labels.items():
        value = _stringify_list(core_variables.get(key, []))
        if value:
            parts.append(f"{label}: {value}")
    return " | ".join(parts)


def _base_paper_columns(summary: Dict[str, Any], paper_info: Mapping[str, Any], preprocess: Mapping[str, Any]) -> Dict[str, Any]:
    authors = paper_info.get("authors", []) if isinstance(paper_info.get("authors"), list) else []
    return {
        "论文标题": paper_info.get("title", ""),
        "作者": ", ".join(authors) if authors else _stringify_list(paper_info.get("authors", [])),
        "发表年份": paper_info.get("year", ""),
        "期刊名称": paper_info.get("journal", ""),
        "DOI": paper_info.get("doi", ""),
        "解析器": preprocess.get("extractor_used", "") or summary.get("engine_used", ""),
        "文本长度": summary.get("text_length", 0),
        "处理状态": summary.get("status", ""),
        "处理时间": summary.get("processing_time", ""),
    }


def _common_analysis_columns(core_analysis: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "研究摘要": core_analysis.get("summary") or "",
        "关键要点": _stringify_list(core_analysis.get("key_points", [])),
        "研究方法": core_analysis.get("methodology") or "",
        "主要发现": core_analysis.get("findings") or "",
        "研究结论": core_analysis.get("conclusions") or "",
        "理论贡献": core_analysis.get("relevance") or "",
        "研究局限": core_analysis.get("limitations") or "",
    }


def _routing_audit_columns(ai_summary: Mapping[str, Any]) -> Dict[str, Any]:
    routing = get_routing(ai_summary)
    quality = get_quality_audit(ai_summary)
    return {
        "论文类型": routing.get("paper_type") or "",
        "子类型标准值": routing.get("paper_subtype_normalized") or "",
        "分类状态": routing.get("classification_status") or "",
        "路由置信度": routing.get("route_confidence") or "",
        "提取置信度": quality.get("extraction_confidence") or "",
        "完整度": quality.get("completeness_score") or 0,
        "是否建议人工复核": bool(quality.get("needs_manual_review")),
        "分类依据": routing.get("classification_rationale") or "",
        "primary_sheet_target": get_primary_sheet_target(ai_summary),
        "缺失关键字段数": len(quality.get("missing_critical_fields", [])),
        "冲突标记": _stringify_list(quality.get("conflict_flags", [])),
    }


def _detailed_json(summary: Dict[str, Any]) -> str:
    return canonical_ai_summary_json(summary)


def _future_research_directions(core_analysis: Mapping[str, Any]) -> str:
    return _stringify_list(core_analysis.get("future_research_directions", []))


def _auto_fit_sheet(worksheet: Any) -> None:
    for column in worksheet.columns:
        cells = list(column)
        max_length = 0
        for cell in cells:
            value = "" if cell.value is None else str(cell.value)
            max_length = max(max_length, len(value))
        worksheet.column_dimensions[cells[0].column_letter].width = min(max_length + 2, 60)


def _build_dataframe(rows: List[Dict[str, Any]], columns: Sequence[str]) -> pd.DataFrame:
    sanitized_rows = [
        {key: _sanitize_excel_value(value) for key, value in row.items()}
        for row in rows
    ]
    return pd.DataFrame(sanitized_rows, columns=cast(Any, list(columns)))


def _workspace_report_path(generator_instance: Any, suffix: str, fallback_name: str) -> str:  # type: ignore
    helper = getattr(generator_instance, "_get_report_file_path", None)
    if callable(helper):
        return str(helper(suffix))
    if getattr(generator_instance, "project_name", None):
        return os.path.join(generator_instance.output_dir, f"{generator_instance.project_name}{suffix}")  # type: ignore
    return os.path.join(generator_instance.output_dir, fallback_name)  # type: ignore


def _write_attempt_history(handle: Any, attempt_history: Sequence[Mapping[str, Any]]) -> None:
    if not attempt_history:
        return

    handle.write("   尝试明细:\n")
    for attempt_index, attempt in enumerate(attempt_history, 1):
        handle.write(
            "     "
            f"[{attempt_index}] 策略={attempt.get('preprocess_strategy', '')} | "
            f"profile={attempt.get('preprocess_profile', '')} | "
            f"parser={attempt.get('parser_mode', '')} | "
            f"提取器={attempt.get('extractor_used', '')} | "
            f"source={attempt.get('selected_text_source', '')} | "
            f"quality={attempt.get('stage1_quality_level', '')} | "
            f"MinerU requested={attempt.get('mineru_remote_requested', '')}, "
            f"enabled={attempt.get('mineru_remote_enabled', '')}, "
            f"attempted={attempt.get('mineru_attempted', '')}, "
            f"succeeded={attempt.get('mineru_succeeded', '')}, "
            f"route={attempt.get('mineru_route', '')} | "
            f"模型={attempt.get('model_used', '')} | "
            f"原因={attempt.get('quality_reason', '')}\n"
        )


def generate_excel_report(generator_instance: Any) -> bool:  # type: ignore
    try:
        generator_instance.logger.info("正在生成多工作表 Excel 分析报告...")  # type: ignore
        summary_file = getattr(generator_instance, "summary_file", None)  # type: ignore
        if not summary_file:
            generator_instance.logger.error("summary_file 属性不存在或为空")  # type: ignore
            return False

        summaries = read_json_robust(summary_file)
        if not summaries:
            generator_instance.logger.warning("没有找到任何摘要数据")  # type: ignore
            return False

        total_rows: List[Dict[str, Any]] = []
        empirical_rows: List[Dict[str, Any]] = []
        review_rows: List[Dict[str, Any]] = []
        conceptual_rows: List[Dict[str, Any]] = []
        uncertain_rows: List[Dict[str, Any]] = []

        for summary in summaries:
            if summary.get("status") != "success":
                continue

            paper_info, core_analysis, routing, specialized, preprocess = _get_summary_sections(summary)
            ai_summary = get_ai_summary(summary)
            base = _base_paper_columns(summary, paper_info, preprocess)
            common = _common_analysis_columns(core_analysis)
            routing_audit = _routing_audit_columns(ai_summary)
            paper_type = str(routing.get("paper_type") or "")
            classification_status = str(routing.get("classification_status") or "")
            future_directions = _future_research_directions(core_analysis)
            detailed_json = _detailed_json(summary)

            total_row = {
                **base,
                **common,
                **routing_audit,
                "理论框架": core_analysis.get("theoretical_framework") or "",
                "研究空白": core_analysis.get("research_gap") or "",
                "未来研究方向": future_directions,
                "detailed_json": detailed_json,
            }
            total_rows.append(total_row)

            if paper_type == "empirical":
                empirical = specialized.get("empirical") or {}
                empirical_rows.append(
                    {
                        **base,
                        **common,
                        **routing_audit,
                        "理论框架": core_analysis.get("theoretical_framework") or "",
                        "研究空白": core_analysis.get("research_gap") or "",
                        "研究问题/假设": _stringify_list(empirical.get("research_questions_or_hypotheses", [])),
                        "数据来源与样本": empirical.get("data_source_and_size") or "",
                        "分析技术": empirical.get("analysis_technique") or "",
                        "核心变量": _format_core_variables(empirical.get("core_variables")),
                        "样本/情境": empirical.get("sample_characteristics_or_context") or "",
                        "未来研究方向": future_directions,
                        "detailed_json": detailed_json,
                    }
                )
            elif paper_type == "review":
                review = specialized.get("review") or {}
                review_rows.append(
                    {
                        **base,
                        **common,
                        **routing_audit,
                        "理论框架": core_analysis.get("theoretical_framework") or "",
                        "研究空白": core_analysis.get("research_gap") or "",
                        "综述类型": review.get("review_type") or "",
                        "检索数据库": _stringify_list(review.get("search_databases", [])),
                        "时间范围": review.get("time_span") or "",
                        "纳入文献数量": review.get("included_studies_count") or "",
                        "纳排标准": review.get("inclusion_exclusion_criteria") or "",
                        "综合方法": review.get("synthesis_approach") or "",
                        "主题聚类": _stringify_list(review.get("main_themes", [])),
                        "未来研究方向": future_directions,
                        "detailed_json": detailed_json,
                    }
                )
            elif paper_type == "conceptual":
                conceptual = specialized.get("conceptual") or {}
                conceptual_rows.append(
                    {
                        **base,
                        **common,
                        **routing_audit,
                        "理论框架": core_analysis.get("theoretical_framework") or "",
                        "研究空白": core_analysis.get("research_gap") or "",
                        "核心命题": _stringify_list(conceptual.get("core_propositions", [])),
                        "概念关系": conceptual.get("conceptual_relationships") or "",
                        "理论贡献（类型详情）": conceptual.get("theoretical_contributions") or "",
                        "未来研究方向": future_directions,
                        "detailed_json": detailed_json,
                    }
                )

            if classification_status != "resolved":
                uncertain_rows.append(
                    {
                        **base,
                        **common,
                        **routing_audit,
                        "理论框架": core_analysis.get("theoretical_framework") or "",
                        "研究空白": core_analysis.get("research_gap") or "",
                        "未来研究方向": future_directions,
                        "detailed_json": detailed_json,
                    }
                )

        excel_file = _workspace_report_path(generator_instance, "_analyzed_papers.xlsx", "analyzed_papers.xlsx")

        sheet_map = {
            "总表": _build_dataframe(total_rows, TOTAL_COLUMNS),
            "实证论文": _build_dataframe(empirical_rows, EMPIRICAL_COLUMNS),
            "综述论文": _build_dataframe(review_rows, REVIEW_COLUMNS),
            "概念论文": _build_dataframe(conceptual_rows, CONCEPTUAL_COLUMNS),
            "未确定论文": _build_dataframe(uncertain_rows, UNCERTAIN_COLUMNS),
        }

        with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:  # type: ignore
            for sheet_name, dataframe in sheet_map.items():
                dataframe.to_excel(writer, sheet_name=sheet_name, index=False)  # type: ignore
                _auto_fit_sheet(writer.sheets[sheet_name])

        generator_instance.logger.success(f"Excel 分析报告已生成: {excel_file}")  # type: ignore
        generator_instance.logger.info(
            f"总表 {len(total_rows)} 篇 | 实证 {len(empirical_rows)} 篇 | 综述 {len(review_rows)} 篇 | "
            f"概念 {len(conceptual_rows)} 篇 | 未确定 {len(uncertain_rows)} 篇"
        )  # type: ignore
        return True
    except Exception as exc:
        generator_instance.logger.error(f"生成 Excel 报告失败: {exc}")  # type: ignore
        return False


def generate_failure_report(generator_instance: Any) -> bool:  # type: ignore
    try:
        failed_papers = getattr(generator_instance, "failed_papers", None)  # type: ignore
        if not failed_papers:
            return True

        failure_report_file = _workspace_report_path(generator_instance, "_failed_papers_report.txt", "failed_papers_report.txt")

        with open(failure_report_file, "w", encoding="utf-8") as handle:
            handle.write("文献综述自动生成器 - 失败报告\n")
            handle.write("=" * 80 + "\n")
            handle.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            handle.write(f"总失败论文数: {len(failed_papers)}\n")
            handle.write("=" * 80 + "\n\n")

            for index, failed_item in enumerate(failed_papers, 1):
                paper = failed_item.get("paper_info", {})
                handle.write(f"{index}. 标题: {paper.get('title', '未知标题')}\n")
                handle.write(f"   作者: {_stringify_list(paper.get('authors', []))}\n")
                handle.write(f"   年份: {paper.get('year', '未知年份')}\n")
                handle.write(f"   期刊: {paper.get('journal', '未知期刊')}\n")
                handle.write(f"   DOI: {paper.get('doi', '')}\n")
                handle.write(f"   失败原因: {failed_item.get('failure_reason', '未知原因')}\n")
                if failed_item.get("pdf_match"):
                    handle.write(
                        "   PDF match: "
                        + json.dumps(failed_item["pdf_match"], ensure_ascii=False, sort_keys=True)
                        + "\n"
                    )
                if failed_item.get("source_identity"):
                    handle.write(
                        "   Source identity: "
                        + json.dumps(failed_item["source_identity"], ensure_ascii=False, sort_keys=True)
                        + "\n"
                    )
                _write_attempt_history(handle, failed_item.get("attempt_history", []))
                handle.write("-" * 60 + "\n")

        generator_instance.logger.success(f"失败报告已生成: {failure_report_file}")  # type: ignore
        return True
    except Exception as exc:
        generator_instance.logger.error(f"生成失败报告失败: {exc}")  # type: ignore
        return False


def generate_retry_zotero_report(generator_instance: Any) -> bool:  # type: ignore
    try:
        failed_papers = getattr(generator_instance, "failed_papers", None)  # type: ignore
        if not failed_papers:
            return True

        retry_report_file = _workspace_report_path(generator_instance, "_zotero_report_for_retry.txt", "zotero_report_for_retry.txt")

        with open(retry_report_file, "w", encoding="utf-8") as handle:
            handle.write("Zotero 报告\n")
            handle.write("=" * 50 + "\n")
            handle.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            handle.write(f"失败论文重跑报告 - 项目: {generator_instance.project_name or '未命名项目'}\n")
            handle.write("=" * 50 + "\n\n")

            for failed_item in failed_papers:
                paper = failed_item.get("paper_info", {})
                authors = _stringify_list(paper.get("authors", [])) or "未知作者"
                year = paper.get("year", "未知年份")
                title = paper.get("title", "未知标题")
                journal = paper.get("journal", "未知期刊")
                doi = paper.get("doi", "")
                failure_reason = failed_item.get("failure_reason", "未知原因")

                handle.write(f"标题: {title}\n")
                handle.write(f"作者: {authors}\n")
                handle.write(f"年份: {year}\n")
                handle.write(f"期刊: {journal}\n")
                handle.write(f"DOI: {doi}\n")
                handle.write(f"失败原因: {failure_reason}\n")
                handle.write("---\n")

        generator_instance.logger.success(f"重跑 Zotero 报告已生成: {retry_report_file}")  # type: ignore
        return True
    except Exception as exc:
        generator_instance.logger.error(f"生成重跑 Zotero 报告失败: {exc}")  # type: ignore
        return False
