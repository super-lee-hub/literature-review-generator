#!/usr/bin/env python3
"""
为政策分析项目生成Excel文件
"""

import os
import json
from typing import Any
from datetime import datetime
import pandas as pd
from openpyxl.styles import Font, Alignment, PatternFill


def create_excel_for_policy_analysis():
    """为政策分析项目创建Excel文件"""
    print("正在为政策分析项目生成Excel文件...")
    
    # 读取政策分析的JSON数据
    summary_file = "output/政策分析/政策分析_summaries.json"
    
    if not os.path.exists(summary_file):
        print(f"❌ 找不到文件: {summary_file}")
        return False
    
    with open(summary_file, 'r', encoding='utf-8') as f:
        summaries = json.load(f)
    
    if not summaries:
        print("❌ 没有找到任何摘要数据")
        return False
    
    print(f"✅ 找到 {len(summaries)} 篇论文")
    
    # 提取并优化数据
    optimized_data: list[dict[str, Any]] = []
    
    for summary in summaries:
        # 检查是否为新的两段式结构
        if 'ai_summary' in summary and 'common_core' in summary['ai_summary']:
            # 新的两段式结构
            common_core = summary['ai_summary']['common_core']
            type_specific = summary['ai_summary'].get('type_specific_details', {})
        elif 'common_core' in summary:
            # 兼容旧的单段式结构（直接字段）
            common_core = summary['common_core']
            type_specific = summary.get('type_specific_details', {})
        else:
            # 兼容旧的单段式结构
            common_core = summary
            type_specific = {}
        
        # 从paper_info提取基本信息
        paper_info = summary.get('paper_info', {})  # type: ignore
        authors = paper_info.get('authors', [])  # type: ignore
        if isinstance(authors, list):
            authors_str = ', '.join(authors)  # type: ignore
        else:
            authors_str = str(authors)
        
        # 创建详细信息JSON字符串
        details_json = json.dumps(type_specific, ensure_ascii=False, indent=2)
        if details_json == "{}" or not details_json.strip():
            details_json = "未提供相关信息"
        
        # 创建记录
        record = {  # type: ignore
            '论文标题': paper_info.get('title', ''),  # type: ignore
            '作者': authors_str,
            '发表年份': paper_info.get('year', ''),  # type: ignore
            '期刊名称': paper_info.get('journal', ''),  # type: ignore
            '文本长度': summary.get('text_length', 0),
            '研究摘要': common_core.get('summary', ''),
            '研究方法': common_core.get('methodology', ''),
            '主要发现': common_core.get('findings', ''),
            '研究结论': common_core.get('conclusions', ''),
            '理论贡献': common_core.get('relevance', ''),
            '研究局限': common_core.get('limitations', ''),
            '处理状态': summary.get('status', ''),
            '处理时间': summary.get('processing_time', ''),
            '处理引擎': '',
            '详细信息': details_json
        }
        optimized_data.append(record)  # type: ignore
    
    # 创建DataFrame
    df = pd.DataFrame(optimized_data)
    
    # 创建Excel文件
    output_file = "output/政策分析/政策分析_analyzed_papers.xlsx"
    
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        # 写入主要数据表
        df.to_excel(writer, sheet_name='论文分析摘要', index=False)  # type: ignore
        
        # 获取工作表
        worksheet = writer.sheets['论文分析摘要']
        
        # 设置列宽
        column_widths = {
            'A': 50,  # 论文标题
            'B': 25,  # 作者
            'C': 12,  # 发表年份
            'D': 20,  # 期刊名称
            'E': 12,  # 文本长度
            'F': 80,  # 研究摘要
            'G': 100, # 研究方法
            'H': 120, # 主要发现
            'I': 120, # 研究结论
            'J': 100, # 理论贡献
            'K': 100, # 研究局限
            'L': 12,  # 处理状态
            'M': 25,  # 处理时间
            'N': 12,  # 处理引擎
            'O': 150  # 详细信息
        }
        
        for col, width in column_widths.items():
            worksheet.column_dimensions[col].width = width
        
        # 设置字体和样式
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
        
        # 应用表头样式
        for cell in worksheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        
        # 设置数据行样式
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        
        # 创建项目统计表
        stats_data = {  # type: ignore
            '统计项目': [
                '总论文数',
                '成功处理',
                '失败处理', 
                '成功率(%)',
                '项目名称',
                '生成时间'
            ],
            '数值': [
                len(summaries),
                len([s for s in summaries if s.get('status') == 'success']),
                len([s for s in summaries if s.get('status') == 'failed']),
                f"{len([s for s in summaries if s.get('status') == 'success']) / len(summaries) * 100:.1f}%",
                '政策分析',
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ]
        }
        
        stats_df = pd.DataFrame(stats_data)
        stats_df.to_excel(writer, sheet_name='项目统计', index=False)  # type: ignore
        
        # 设置统计表样式
        stats_worksheet = writer.sheets['项目统计']
        for cell in stats_worksheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        
        for cell in stats_worksheet['B']:
            cell.alignment = Alignment(horizontal="left", vertical="center")
    
    print(f"✅ Excel报告已生成: {output_file}")
    print(f"📊 共包含 {len(summaries)} 篇论文，15 个字段")
    print("📋 包含2个工作表：论文分析摘要 + 项目统计")
    
    return True


if __name__ == "__main__":
    success = create_excel_for_policy_analysis()
    if success:
        print("✅ 政策分析项目Excel文件生成完成!")
    else:
        print("❌ 政策分析项目Excel文件生成失败!")