"""
报告生成模块
负责生成Excel分析报告、失败论文报告和重跑报告
"""

import os
import json
from typing import Any, Dict, List
from datetime import datetime
import pandas as pd  # type: ignore


def read_json_robust(file_path: str) -> Any:
    """
    鲁棒性JSON读取函数，替代encoding_utils中的函数
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                content = f.read()
                return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                return json.loads(content)
    except Exception:
        return []


def generate_excel_report(generator_instance: Any) -> bool:  # type: ignore
    """生成Excel格式的分析报告（优化版本 - 去除重复列）"""
    try:
        generator_instance.logger.info("正在生成Excel分析报告（优化版本 - 去除重复列）...")  # type: ignore
        
        # 添加summary_file None安全检查
        summary_file = getattr(generator_instance, 'summary_file', None)  # type: ignore
        if not summary_file:
            generator_instance.logger.error("summary_file属性不存在或为空")  # type: ignore
            return False
        
        # 读取summaries.json文件（使用robust编码处理）
        summaries = read_json_robust(summary_file)
        
        if not summaries:
            generator_instance.logger.warn("没有找到任何摘要数据")  # type: ignore
            return False
        
        # 提取并优化common_core数据
        optimized_data: list[dict[str, Any]] = []
        
        for summary in summaries:
            # 检查是否为新的两段式结构（正确的路径）
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
            
            # 创建优化的记录，避免重复信息，优先从paper_info提取
            optimized_record: dict[str, Any] = {
                # 论文基本信息（核心字段）- 优先从paper_info提取，备选从common_core提取
                '论文标题': summary.get('paper_info', {}).get('title', '') or common_core.get('title', ''),
                '作者': ', '.join(summary.get('paper_info', {}).get('authors', [])) if summary.get('paper_info', {}).get('authors') else (', '.join(common_core.get('authors', [])) if common_core.get('authors') else ''),
                '发表年份': summary.get('paper_info', {}).get('year', '') or common_core.get('year', ''),
                '期刊名称': summary.get('paper_info', {}).get('journal', '') or common_core.get('journal', ''),
                '文本长度': summary.get('text_length', 0),
                
                # 核心分析内容
                '研究摘要': common_core.get('summary', ''),
                '研究方法': common_core.get('methodology', ''),
                '主要发现': common_core.get('findings', ''),
                '研究结论': common_core.get('conclusions', ''),
                '理论贡献': common_core.get('relevance', ''),
                '研究局限': common_core.get('limitations', ''),
                
                # 处理状态信息
                '处理状态': summary.get('status', ''),
                '处理时间': summary.get('processing_time', ''),
                '处理引擎': summary.get('engine_used', ''),
                
                # 详细信息（JSON格式）
                '详细信息': json.dumps(type_specific, ensure_ascii=False, indent=2)
            }
            
            optimized_data.append(optimized_record)
        
        # 生成Excel文件路径（添加项目名称前缀）
        if generator_instance.project_name:  # type: ignore
            excel_file = os.path.join(generator_instance.output_dir, f'{generator_instance.project_name}_analyzed_papers.xlsx')  # type: ignore
        else:
            excel_file = os.path.join(generator_instance.output_dir, 'analyzed_papers.xlsx')  # type: ignore
        
        # 创建主数据框
        df_main: pd.DataFrame = pd.DataFrame(optimized_data)
        
        # 创建项目统计信息
        success_count = len([s for s in summaries if s.get('status') == 'success'])
        failed_count = len([s for s in summaries if s.get('status') == 'failed'])
        total_count = len(summaries)
        
        stats_data: Dict[str, List[Any]] = {
            '统计项目': [
                '总论文数',
                '成功处理',
                '失败处理', 
                '成功率(%)',
                '项目名称',
                '生成时间'
            ],
            '数值': [
                total_count,
                success_count,
                failed_count,
                f"{success_count / total_count * 100:.1f}%" if total_count > 0 else "0%",
                generator_instance.project_name or "未命名项目",  # type: ignore
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ]
        }
        df_stats: pd.DataFrame = pd.DataFrame(stats_data)
        
        # 保存到Excel（包含多个工作表）
        with pd.ExcelWriter(excel_file, engine='openpyxl') as writer:  # type: ignore
            # 主工作表：论文分析摘要
            df_main.to_excel(writer, sheet_name='论文分析摘要', index=False)  # type: ignore
            
            # 统计工作表：项目概览
            df_stats.to_excel(writer, sheet_name='项目统计', index=False)  # type: ignore
            
            # 格式化工作表
            worksheet = writer.sheets['论文分析摘要']
            for column in worksheet.columns:
                max_length = 0
                column = [cell for cell in column]
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)  # 最大宽度50
                worksheet.column_dimensions[column[0].column_letter].width = adjusted_width
        
        generator_instance.logger.success(f"Excel分析报告已生成: {excel_file}")  # type: ignore
        generator_instance.logger.info(f"共包含 {len(df_main)} 篇论文，{len(df_main.columns)} 个核心字段")  # type: ignore
        generator_instance.logger.info("已去除重复列，包含2个工作表：论文分析摘要 + 项目统计")  # type: ignore
        return True
        
    except Exception as e:
        generator_instance.logger.error(f"生成Excel报告失败: {e}")  # type: ignore
        return False


def generate_failure_report(generator_instance: Any) -> bool:  # type: ignore
    """生成失败论文报告（包含详细失败原因）"""
    try:
        # 添加failed_papers None安全检查
        failed_papers = getattr(generator_instance, 'failed_papers', None)  # type: ignore  # type: ignore  # type: ignore  # type: ignore
        if not failed_papers:
            return True  # 没有失败论文，无需生成报告
        
        # 生成失败报告文件路径（添加项目名称前缀）
        if generator_instance.project_name:  # type: ignore
            failure_report_file = os.path.join(generator_instance.output_dir, f'{generator_instance.project_name}_failed_papers_report.txt')  # type: ignore
        else:
            failure_report_file = os.path.join(generator_instance.output_dir, 'failed_papers_report.txt')  # type: ignore
        
        with open(failure_report_file, 'w', encoding='utf-8') as f:
            f.write("文献综述生成器 - 失败报告\n")
            f.write("=" * 80 + "\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"总失败论文数: {len(generator_instance.failed_papers)}\n")  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore
            f.write(f"项目命名空间: {generator_instance.project_name}\n")  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore  # type: ignore
            f.write("=" * 80 + "\n\n")
            
            for i, failed_item in enumerate(failed_papers, 1):
                paper = failed_item.get('paper_info', {})
                _failure_reason = failed_item.get('failure_reason', '未知原因')  # type: ignore
                
                title = paper.get('title', '未知标题')
                authors = ', '.join(paper.get('authors', [])) if paper.get('authors') else '未知作者'
                year = paper.get('year', '未知年份')
                journal = paper.get('journal', '未知期刊')
                doi = paper.get('doi', '无DOI')
                
                f.write(f"{i}. 📄 标题: {title}\n")
                f.write(f"   👥 作者: {authors}\n")
                f.write(f"   📅 年份: {year}\n")
                f.write(f"   📰 期刊: {journal}\n")
                f.write(f"   🔗 DOI: {doi}\n")
                f.write(f"   ❌ 失败原因: {_failure_reason}\n")  # type: ignore
                f.write("-" * 60 + "\n\n")
            
            f.write("\n🔧 失败原因分类与解决建议：\n")
            f.write("=" * 60 + "\n")
            f.write("1. 【文件查找失败】→ 在Zotero中检查PDF文件是否存在，或尝试手动选择最佳版本\n")
            f.write("2. 【PDF文本提取失败】→ 文件或为扫描版或图片型PDF，需要OCR处理\n")
            f.write("3. 【主引擎调用失败】→ 检查网络连接和主API配置，或稍后重试\n")
            f.write("4. 【备用引擎调用失败】→ 检查备用API配置，或论文过长超出所有引擎限制\n")
            f.write("5. 【调度失败】→ 论文过长，超出所有引擎TPM限制，需要简化或分段处理\n")
            f.write("6. 【处理过程异常】→ 记录具体错误信息，联系技术支持\n\n")
            
            f.write("🚀 分级调度工作流：\n")
            f.write("=" * 60 + "\n")
            f.write("1. 📋 系统会自动为超长论文切换到备用引擎\n")
            f.write("2. 🔄 如果备用引擎也无法处理，才需要人工干预\n")
            f.write("3. ⚙️  使用自动生成的zotero_report_for_retry.txt文件\n")
            f.write("4. 🏃 重新运行程序处理失败论文\n")
            f.write("5. 📊 使用 --merge 命令合并结果回主文件\n")
            f.write("6. ✨ 使用分级调度功能！\n")
        
        generator_instance.logger.success(f"失败报告已生成: {failure_report_file}")  # type: ignore
        generator_instance.logger.info(f"详细记录了 {len(generator_instance.failed_papers)} 篇论文的失败原因")  # type: ignore
        return True
        
    except Exception as e:
        generator_instance.logger.error(f"生成失败报告失败: {e}")  # type: ignore
        return False


def generate_retry_zotero_report(generator_instance: Any) -> bool:  # type: ignore
    """
    生成用于重跑的Zotero报告
    将失败的论文逆向工程成Zotero原始报告格式
    """
    try:
        # 添加failed_papers None安全检查
        failed_papers = getattr(generator_instance, 'failed_papers', None)  # type: ignore
        if not failed_papers:
            return True  # 没有失败论文，无需生成重跑报告
        
        # 生成重跑报告文件路径（添加项目名称前缀）
        if generator_instance.project_name:  # type: ignore
            retry_report_file = os.path.join(generator_instance.output_dir, f'{generator_instance.project_name}_zotero_report_for_retry.txt')  # type: ignore
        else:
            retry_report_file = os.path.join(generator_instance.output_dir, 'zotero_report_for_retry.txt')  # type: ignore
        
        generator_instance.logger.info("正在生成重跑报告...")  # type: ignore
        
        with open(retry_report_file, 'w', encoding='utf-8') as f:
            # 使用标准Zotero报告格式，不带表情符号
            f.write("Zotero 报告\n")
            f.write("=" * 50 + "\n")
            f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"失败论文重跑报告 - 项目: {generator_instance.project_name}\n")  # type: ignore
            f.write("=" * 50 + "\n\n")
            
            for i, failed_item in enumerate(failed_papers, 1):
                paper = failed_item.get('paper_info', {})
                _failure_reason = failed_item.get('failure_reason', '未知原因')  # type: ignore
                
                title = paper.get('title', '')
                authors = paper.get('authors', [])
                year = paper.get('year', '')
                journal = paper.get('journal', '')
                doi = paper.get('doi', '')
                
                # 标准Zotero格式：作者, 年份. 标题. 期刊. DOI: doi
                author_str = ', '.join(authors) if authors else '未知作者'
                year_str = year if year else '未知年份'
                title_str = title if title else '未知标题'
                journal_str = journal if journal else '未知期刊'
                
                f.write(f"{i}. {author_str}, {year_str}. {title_str}. {journal_str}")
                
                if doi:
                    f.write(f". DOI: {doi}")
                
                f.write("\n")
            
            f.write(f"\n统计信息:\n")
            f.write(f"总失败论文数: {len(generator_instance.failed_papers)}\n")  # type: ignore
            f.write(f"项目命名空间: {generator_instance.project_name}\n")  # type: ignore
            f.write("\n使用说明:\n")
            f.write("1. 将此文件路径填入config.ini的zotero_report配置项\n")
            f.write("2. 重新运行程序专门处理这些失败的论文\n")
            f.write("3. 使用 --merge 命令合并处理结果\n")
        
        generator_instance.logger.success(f"重跑报告已生成: {retry_report_file}")  # type: ignore
        generator_instance.logger.info(f"已为 {len(generator_instance.failed_papers)} 篇失败论文生成重跑报告")  # type: ignore
        return True
        
    except Exception as e:
        generator_instance.logger.error(f"生成重跑报告失败: {e}")  # type: ignore
        return False