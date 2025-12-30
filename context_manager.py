"""
上下文管理模块 - 用于优化大文本处理和上下文截断
作者：Python资深架构师
日期：2025-11-30
"""

import json
import re
import warnings
from typing import List, Dict, Any, Tuple, Union

# 导入项目类型定义
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import ProcessingResult


def estimate_tokens(text: str) -> int:
    """
    估算文本的Token数量（粗略估算）
    英文：1 token ≈ 4字符
    中文：1 token ≈ 1字符
    """
    if not text:
        return 0
    
    # 统计中英文字符
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_chars = len(re.findall(r'[a-zA-Z]', text))
    
    # 估算token数
    estimated_tokens = chinese_chars + (english_chars / 4)
    return int(estimated_tokens)


def convert_json_to_markdown(summaries_data: Union[List[Dict[str, Any]], List[ProcessingResult]]) -> str:
    """
    将JSON摘要列表转换为紧凑的Markdown格式
    
    Args:
        summaries_data: JSON格式的论文摘要列表
        
    Returns:
        Markdown格式的文献综述数据
    """
    if not summaries_data:
        return "# 文献综述数据\\n\\n（暂无数据）"
    
    markdown_content = "# 文献综述数据\\n\\n"
    
    for i, summary in enumerate(summaries_data, 1):
        try:
            # 获取paper_info
            paper_info = summary.get('paper_info', {})  # type: ignore
            
            # 获取AI摘要内容
            ai_summary = summary.get('ai_summary', {})  # type: ignore
            common_core = ai_summary.get('common_core', {}) if ai_summary else {}  # type: ignore
            
            # 提取信息
            title = paper_info.get('title', '未知标题')  # type: ignore
            year = paper_info.get('year', '未知年份')  # type: ignore
            authors = paper_info.get('authors', '未知作者')  # type: ignore
            
            # 处理作者列表
            if isinstance(authors, list):  # type: ignore
                authors_str = ', '.join(authors)  # type: ignore
            elif isinstance(authors, str):  # type: ignore
                authors_str = authors  # type: ignore
            else:
                authors_str = '未知作者'
            
            # 构建Markdown条目
            markdown_content += f"## 文献{i}: {title}\\n\\n"
            markdown_content += f"**作者**: {authors_str} ({year})\\n\\n"
            
            # 摘要
            summary_text = common_core.get('summary', '暂无摘要')  # type: ignore
            if summary_text and summary_text != '...' and summary_text.strip():  # type: ignore
                markdown_content += f"**摘要**: {summary_text}\\n\\n"
            
            # 主要发现
            findings = common_core.get('findings', '暂无发现')  # type: ignore
            if findings and findings != '...' and findings.strip():  # type: ignore
                markdown_content += f"**主要发现**: {findings}\\n\\n"
            
            # 方法论
            methodology = common_core.get('methodology', '暂无方法')  # type: ignore
            if methodology and methodology != '...' and methodology.strip():  # type: ignore
                markdown_content += f"**方法论**: {methodology}\\n\\n"
            
            # 核心观点
            key_points = common_core.get('key_points', [])  # type: ignore
            if key_points and isinstance(key_points, list) and any(kp.strip() and kp != '...' for kp in key_points):  # type: ignore
                markdown_content += "**核心观点**:\\n"
                for j, point in enumerate(key_points, 1):  # type: ignore
                    if point and point.strip() and point != '...':  # type: ignore
                        markdown_content += f"{j}. {point}\\n"
                markdown_content += "\\n"
            
            markdown_content += "---\\n\\n"
            
        except Exception as e:
            # 如果某条记录处理出错，记录并继续处理下一条
            warnings.warn(f"处理文献{i}时出错: {str(e)}")
            markdown_content += f"## 文献{i}: [处理出错]\\n\\n"
            markdown_content += f"错误信息: {str(e)}\\n\\n"
            markdown_content += "---\\n\\n"
    
    return markdown_content


def truncate_context_if_needed(text: str, max_tokens: int = 950000) -> Tuple[str, bool]:
    """
    智能截断上下文，防止API超时
    
    Args:
        text: 原始文本
        max_tokens: 最大token数限制（Gemini 3 Pro有1M token，使用950000作为安全阈值，仅在最极端情况下触发）
        
    Returns:
        Tuple[截断后的文本, 是否被截断]
    """
    current_tokens = estimate_tokens(text)
    
    if current_tokens <= max_tokens:
        return text, False
    
    # 需要截断的情况
    warnings.warn(f"文本token数({current_tokens})超过限制({max_tokens})，将进行智能截断")
    
    # 保留策略：
    # 1. 头部指令（假设前5000字符为重要指令）
    # 2. 尾部最新文献（假设最后30000字符为最新内容）
    # 3. 中间部分截断
    
    head_limit = 5000
    tail_limit = 30000
    
    head_text = text[:head_limit]
    
    # 找到适当的分割点（在tail_limit范围内）
    split_start = len(text) - tail_limit
    if split_start < head_limit:
        # 如果文本太短，在中间分割
        mid_point = len(text) // 2
        tail_text = text[mid_point:]
    else:
        tail_text = text[split_start:]
    
    # 连接截断后的文本
    truncated_text = f"{head_text}\\n\\n[... 中间内容已截断 ...]\\n\\n{tail_text}"
    
    final_tokens = estimate_tokens(truncated_text)
    
    warnings.warn(f"截断完成：原始{current_tokens}tokens -> {final_tokens}tokens")
    warnings.warn(f"保留比例：{final_tokens/current_tokens:.1%}")
    
    return truncated_text, True


def validate_summary_quality(summary_data: Union[Dict[str, Any], ProcessingResult]) -> Tuple[bool, str]:
    """
    检查摘要质量，识别空字段或不完整内容，特别针对占位符内容
    
    Args:
        summary_data: 论文摘要数据
        
    Returns:
        Tuple[是否质量合格, 不合格原因]
    """
    if not summary_data:
        return False, "摘要数据为空"
    
    try:
        # 获取AI摘要数据
        ai_summary = summary_data.get('ai_summary', {})  # type: ignore
        
        common_core: Any = ai_summary.get('common_core', {}) if ai_summary else {}
        
        # 定义无效内容的关键词黑名单（扩展版本）
        PLACEHOLDER_KEYWORDS = [
            "未提供相关信息", "未提及", "未提供", "无相关信息", "未知", 
            "Not provided", "N/A", "null", "None", "...", "无摘要", "无数据",
            "暂无信息", "信息不完整", "未明确说明", "无具体说明"
        ]
        
        # 检查关键字段
        issues = []
        
        # 检查摘要
        summary = common_core.get('summary', '')  # type: ignore
        summary_text = str(summary).strip()  # type: ignore
        if not summary_text or summary_text == '' or summary_text == '...':
            issues.append("摘要为空")  # type: ignore
        elif len(summary_text) < 50:  # type: ignore
            issues.append("摘要过短(<50字)")  # type: ignore
        # 检查是否包含占位符关键词
        elif any(keyword in summary_text for keyword in PLACEHOLDER_KEYWORDS):  # type: ignore
            # 如果内容很短且包含占位符关键词，强烈怀疑是占位符
            if len(summary_text) < 30:  # type: ignore
                issues.append(f"摘要包含无效占位符: '{summary_text}'")  # type: ignore
            else:
                # 如果内容较长但包含占位符，需要更严格的检查
                placeholder_count = sum(1 for keyword in PLACEHOLDER_KEYWORDS if keyword in summary_text)  # type: ignore
                if placeholder_count > 0 and len(summary_text) < 100:  # type: ignore
                    issues.append(f"摘要可能包含占位符内容")  # type: ignore
        
        # 检查核心观点
        key_points = common_core.get('key_points', [])  # type: ignore
        if not key_points or not isinstance(key_points, list):  # type: ignore
            issues.append("核心观点字段格式错误")  # type: ignore
        else:
            valid_points = []  # type: ignore
            for kp in key_points:  # type: ignore
                kp_text = str(kp).strip()  # type: ignore
                if kp_text and kp_text != '...' and kp_text not in PLACEHOLDER_KEYWORDS:  # type: ignore
                    valid_points.append(kp_text)  # type: ignore
            
            if not valid_points:  # type: ignore
                issues.append("核心观点全部为空或为占位符")  # type: ignore
            elif len(valid_points) < len(key_points):  # type: ignore
                issues.append(f"核心观点中存在占位符内容")  # type: ignore
        
        # 检查主要发现
        findings = common_core.get('findings', '')  # type: ignore
        findings_text = str(findings).strip()  # type: ignore
        if not findings_text or findings_text == '' or findings_text == '...':
            issues.append("主要发现为空")  # type: ignore
        elif len(findings_text) < 50:  # type: ignore
            issues.append("主要发现过短(<50字)")  # type: ignore
        elif any(keyword in findings_text for keyword in PLACEHOLDER_KEYWORDS):  # type: ignore
            if len(findings_text) < 30:  # type: ignore
                issues.append(f"主要发现包含无效占位符")  # type: ignore
        
        # 检查结论
        conclusions = common_core.get('conclusions', '')  # type: ignore
        conclusions_text = str(conclusions).strip()  # type: ignore
        if conclusions_text and any(keyword in conclusions_text for keyword in PLACEHOLDER_KEYWORDS):  # type: ignore
            if len(conclusions_text) < 30:  # type: ignore
                issues.append(f"结论包含无效占位符")  # type: ignore
        
        # 检查理论贡献
        relevance = common_core.get('relevance', '')  # type: ignore
        relevance_text = str(relevance).strip()  # type: ignore
        if relevance_text and any(keyword in relevance_text for keyword in PLACEHOLDER_KEYWORDS):  # type: ignore
            if len(relevance_text) < 30:  # type: ignore
                issues.append(f"理论贡献包含无效占位符")  # type: ignore
        
        # 检查研究局限
        limitations = common_core.get('limitations', '')  # type: ignore
        limitations_text = str(limitations).strip()  # type: ignore
        if limitations_text and any(keyword in limitations_text for keyword in PLACEHOLDER_KEYWORDS):  # type: ignore
            if len(limitations_text) < 30:  # type: ignore
                issues.append(f"研究局限包含无效占位符")  # type: ignore
        
        # 检查元数据质量
        authors = common_core.get('authors', [])  # type: ignore
        if not authors or (isinstance(authors, list) and len(authors) == 0):  # type: ignore
            issues.append("作者信息缺失")  # type: ignore
        
        year = common_core.get('year', '')  # type: ignore
        if str(year) in ['未知年份', '未知', ''] or not str(year).strip():  # type: ignore
            issues.append("年份信息缺失")  # type: ignore
        
        journal = common_core.get('journal', '')  # type: ignore
        if str(journal) in ['未知期刊', '未知', ''] or not str(journal).strip():  # type: ignore
            issues.append("期刊信息缺失")  # type: ignore
        
        if issues:  # type: ignore
            return False, "; ".join(issues)  # type: ignore
        else:
            return True, "质量检查通过"
            
    except Exception as e:
        return False, f"质量检查异常: {str(e)}"


def optimize_context_for_synthesis(summaries_data: Union[List[Dict[str, Any]], List[ProcessingResult]],
                                   outline: str, 
                                   max_tokens: int = 950000) -> str:
    """
    优化综述生成的上下文数据
    
    Args:
        summaries_data: 文献摘要列表
        outline: 综述大纲
        max_tokens: 最大token数（Gemini 3 Pro有1M token，使用950000作为安全阈值）
        
    Returns:
        优化后的上下文文本
    """    # 转换为Markdown
    markdown_data = convert_json_to_markdown(summaries_data)
    
    # 构建完整上下文
    full_context = f"""# 综述写作上下文

## 综述大纲
{outline}

## 文献分析数据
{markdown_data}

## 写作要求
请基于上述大纲和文献数据，撰写符合学术规范的综述内容。
每个论点必须引用至少1-2篇文献，格式为(作者, 年份)。
"""
    
    # 智能截断
    optimized_context, _ = truncate_context_if_needed(full_context, max_tokens)
    
    return optimized_context


def optimize_context_for_outline(summaries_data: List[ProcessingResult], max_tokens: int = 950000) -> str:
    """
    优化大纲生成的上下文数据（使用高密度Markdown格式，提取全部有效字段）
    
    Args:
        summaries_data: 文献摘要列表（ProcessingResult格式）
        max_tokens: 最大令牌数限制（Gemini 3 Pro有1M token，使用950000作为安全阈值）
        
    Returns:
        优化后的上下文字符串
    """
    # 转换为高密度Markdown格式（与综述生成使用相同的压缩策略）
    markdown_data = convert_json_to_markdown(summaries_data)
    
    # 构建优化后的上下文（去除JSON结构开销，使用纯文本格式）
    optimized_context = f"""# 文献综述大纲生成数据

## 文献分析摘要（共{len([s for s in summaries_data if s.get('status') == 'success'])}篇成功文献）
{markdown_data}

## 大纲生成要求
请基于上述文献分析数据，生成一份结构完整的文献综述大纲。

大纲应包含：
1. 清晰的章节结构（使用#、##、###等Markdown标题）
2. 每个章节下列出核心论点和分析要点
3. 合理组织文献，体现研究主题的发展脉络
4. 突出关键概念、理论框架和研究方法
"""
    
    # 智能截断（仅当绝对必要时）
    optimized_context, was_truncated = truncate_context_if_needed(optimized_context, max_tokens)
    
    if was_truncated:
        warnings.warn(f"大纲生成上下文被截断，原始token数可能超过{max_tokens}")
    
    return optimized_context


def batch_quality_check(summaries_data: Union[List[Dict[str, Any]], List[ProcessingResult]]) -> Dict[str, Any]:
    """
    批量质量检查
    
    Args:
        summaries_data: 文献摘要列表
        
    Returns:
        质量检查报告
    """
    report = {  # type: ignore
        "total_papers": len(summaries_data),
        "qualified_papers": 0,
        "failed_papers": [],
        "quality_issues": {}
    }
    
    for i, summary in enumerate(summaries_data):  # type: ignore
        try:
            # 跳过失败的ProcessingResult对象
            if summary.get('status') == 'failed':  # type: ignore
                report["failed_papers"].append({  # type: ignore
                    "index": i,
                    "reason": "Processing result status is failed"
                })
                continue
                
            is_qualified, reason = validate_summary_quality(summary)  # type: ignore
            
            if is_qualified:  # type: ignore
                report["qualified_papers"] += 1  # type: ignore
            else:
                report["failed_papers"].append({  # type: ignore
                    "index": i,
                    "reason": reason
                })
                
                # 统计问题类型
                issue_type = reason.split(";")[0]  # type: ignore
                report["quality_issues"][issue_type] = \
                    report["quality_issues"].get(issue_type, 0) + 1  # type: ignore
                    
        except Exception as e:
            report["failed_papers"].append({  # type: ignore
                "index": i,
                "reason": f"检查异常: {str(e)}"
            })  # type: ignore
    
    return report  # type: ignore


if __name__ == "__main__":
    # 测试用例
    test_summaries = [  # type: ignore
        {
            "paper_info": {
                "title": "测试论文1",
                "year": "2023",
                "authors": ["张三", "李四"]
            },
            "ai_summary": {
                "common_core": {
                    "summary": "这是一篇关于测试的研究...",
                    "key_points": ["核心观点1", "核心观点2"],
                    "findings": "主要发现内容..."
                }
            }
        }
    ]
    
    # 测试Markdown转换
    markdown_result = convert_json_to_markdown(test_summaries)  # type: ignore
    print("Markdown转换测试:")
    print(markdown_result)
    
    # 测试质量检查
    quality_report = batch_quality_check(test_summaries)  # type: ignore
    print("\\n质量检查报告:")
    print(json.dumps(quality_report, ensure_ascii=False, indent=2))