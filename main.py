#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
文献综述自动生成器 - 工业级版本
支持身份基断点续传、双重工作模式、智能续写、项目命名空间、智能文件查找、双引擎PDF提取、适应性速率控制、并发处理、错误管理、自动重试机制和交互式安装向导。

作者: llm_reviewer_generator 文献综述自动生成器开发团队
版本: 1.2
更新日期: 2025-10-15
"""

import sys
import os
import time
import argparse
import traceback
import concurrent.futures
import threading
import json
import logging
from typing import List, Dict, Any, Optional, Set, Tuple, Iterator, Union
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 导入项目模块
from models import (
    PaperInfo, ProcessingResult, FailedPaper, SummariesList,
    APIConfig, AISummary
)
from config_loader import load_config, ConfigDict
from zotero_parser import parse_zotero_report
from file_finder import create_file_index, FileIndex, find_pdf
from pdf_extractor import extract_text_from_pdf  # type: ignore
from ai_interface import get_summary_from_ai, get_concept_analysis, _call_ai_api  # type: ignore
from docx_writer import create_word_document, append_section_to_word_document, generate_word_table_of_contents, generate_apa_references
from report_generator import generate_excel_report, generate_failure_report, generate_retry_zotero_report  # type: ignore
from utils import ensure_dir, sanitize_path_component
from setup_wizard import run_setup_wizard
import validator



# 优雅地处理可选依赖
try:
    from docx import Document  # type: ignore
    from docx.shared import Pt  # type: ignore
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT  # type: ignore
    from docx.oxml.ns import qn  # type: ignore
    _docx_available = True
except ImportError:
    _docx_available = False
    Document = None  # type: ignore
    Pt = None  # type: ignore
    WD_PARAGRAPH_ALIGNMENT = None  # type: ignore
    qn = None  # type: ignore

# 定义常量，避免重定义问题
DOCX_AVAILABLE = _docx_available

try:
    from tqdm import tqdm  # type: ignore
    _tqdm_available = True
except ImportError:
    _tqdm_available = False
    # 创建一个假的tqdm类以避免在代码中进行大量的if检查
    class tqdm:
        def __init__(self, iterable: Optional[Any] = None, **kwargs: Any) -> None:
            self.iterable: List[Any] = iterable if iterable else []
        def __iter__(self) -> Iterator[Any]:
            return iter(self.iterable)
        def __enter__(self) -> 'tqdm':
            return self
        def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
            pass
        def update(self, n: int = 1) -> None:
            pass
        def set_postfix_str(self, s: str) -> None:
            pass
        def close(self) -> None:
            pass

# 定义常量，避免重定义问题
TQDM_AVAILABLE = _tqdm_available

# 在文件开头打印警告信息（使用logging而不是print）
if not DOCX_AVAILABLE:
    logging.warning("未安装 'python-docx'。生成Word文档和第二阶段验证功能将不可用。请运行: pip install python-docx")
if not TQDM_AVAILABLE:
    logging.warning("未安装 'tqdm'。将无法显示进度条。请运行: pip install tqdm")

class CustomLogger(logging.Logger):
    def success(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.info(f"[SUCCESS] {msg}", *args, **kwargs)
    def warn(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self.warning(f"[WARN] {msg}", *args, **kwargs)

logging.setLoggerClass(CustomLogger)

# ==========================================================

class Counter:
    """线程安全计数器 - 简化版本提高性能"""
    def __init__(self, initial_value: int = 0):
        self._value = initial_value
        self._lock = threading.Lock()

    def increment(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    def decrement(self) -> int:
        with self._lock:
            self._value -= 1
            return self._value

    @property
    def value(self) -> int:
        """获取当前值（属性方式访问）"""
        with self._lock:
            return self._value

    def set(self, new_value: int) -> None:
        """设置值，确保线程安全"""
        with self._lock:
            self._value = new_value

    def get_value(self) -> int:
        """获取当前值，避免属性装饰器的开销"""
        with self._lock:
            return self._value

    def set_value(self, new_value: int) -> None:
        """设置值，确保线程安全"""
        with self._lock:
            self._value = new_value

class ReportingService:
    """报告生成服务 - 专门负责生成各种分析报告"""

    def __init__(self, logger: CustomLogger):
        self.logger: CustomLogger = logger if logger is not None else logging.getLogger(__name__)  # type: ignore

    def generate_all_reports(self, generator: 'LiteratureReviewGenerator') -> None:
        """生成所有分析阶段的报告"""
        self.logger.info("正在生成所有分析报告...")

        # 生成Excel报告
        if not generate_excel_report(generator):
            self.logger.warning("Excel报告生成失败，但不影响整体处理结果")

        # 生成失败报告（如果有失败的论文）
        if generator.failed_papers:
            if not generate_failure_report(generator):
                self.logger.warning("失败报告生成失败，但不影响整体处理结果")

        # 只在Zotero模式下生成自动化重跑报告
        if generator.mode == "zotero" and generator.failed_papers:
            if not generate_retry_zotero_report(generator):
                self.logger.warning("重跑报告生成失败，但不影响整体处理结果")

        self.logger.success("所有分析报告生成完毕。")


class CheckpointManager:
    """检查点管理器 - 专门负责处理基于身份的断点续传"""

    def __init__(self, logger: CustomLogger):
        self.logger: CustomLogger = logger or logging.getLogger(__name__)  # type: ignore

    def save_checkpoint(self, generator: 'LiteratureReviewGenerator') -> bool:
        """保存基于身份的断点文件"""
        try:
            if not generator.output_dir or not generator.project_name:
                return False

            checkpoint_file = os.path.join(generator.output_dir, f'{generator.project_name}_checkpoint.json')

            # 创建已处理论文的身份集合
            processed_papers: Set[str] = set()
            for summary in generator.summaries:
                if summary.get('status') == 'success':
                    paper_info: PaperInfo = summary.get('paper_info', {})  # type: ignore
                    paper_key: str = LiteratureReviewGenerator.get_paper_key(paper_info)  # type: ignore
                    processed_papers.add(paper_key)

            # 创建失败论文的身份集合
            failed_papers: Set[str] = set()
            for failed_item in generator.failed_papers:
                paper_info: PaperInfo = failed_item.get('paper_info', {})  # type: ignore
                paper_key: str = LiteratureReviewGenerator.get_paper_key(paper_info)  # type: ignore
                failed_papers.add(paper_key)

            checkpoint_data: Dict[str, Any] = {
                'version': '2.0',  # 身份基断点版本
                'project_name': generator.project_name,
                'update_time': datetime.now().isoformat(),
                'total_papers': len(generator.papers),
                'processed_count': len(processed_papers),
                'failed_count': len(failed_papers),
                'processed_papers': list(processed_papers),  # 基于身份的已处理列表
                'failed_papers': list(failed_papers),        # 基于身份的失败列表
                'processing_stats': {
                    'processed_success': generator.processed_count.value,
                    'failed_attempts': generator.failed_count.value
                }
            }

            with open(checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(checkpoint_data, f, ensure_ascii=False, indent=2)

            self.logger.info(f"[断点保存] 已保存处理进度: {len(processed_papers)}成功, {len(failed_papers)}失败")
            return True

        except Exception as e:
            self.logger.error(f"保存断点文件失败: {e}")
            return False

    def load_checkpoint(self, generator: 'LiteratureReviewGenerator') -> bool:
        """加载基于身份的断点文件"""
        try:
            if not generator.output_dir or not generator.project_name:
                return False

            checkpoint_file = os.path.join(generator.output_dir, f'{generator.project_name}_checkpoint.json')

            if not os.path.exists(checkpoint_file):
                self.logger.info("[断点加载] 未找到断点文件，将开始全新处理")
                return False

            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                checkpoint_data: Dict[str, Any] = json.load(f)

            # 验证断点文件版本
            version = checkpoint_data.get('version', '1.0')
            if version != '2.0':
                self.logger.warning(f"[断点加载] 检测到旧版本断点文件(v{version})，将开始全新处理")
                return False

            # 验证项目名称匹配
            checkpoint_project = checkpoint_data.get('project_name')
            if checkpoint_project != generator.project_name:
                self.logger.warning(f"[断点加载] 项目名称不匹配({checkpoint_project} != {generator.project_name})，将开始全新处理")
                return False

            # 提取已处理和失败的论文身份
            processed_papers = set(checkpoint_data.get('processed_papers', []))
            failed_papers = set(checkpoint_data.get('failed_papers', []))
            update_time = checkpoint_data.get('update_time', '未知时间')

            self.logger.info(f"[断点加载] 成功加载断点文件 (更新时间: {update_time})")
            self.logger.info(f"[断点加载] 已处理论文: {len(processed_papers)}篇")
            self.logger.info(f"[断点加载] 失败论文: {len(failed_papers)}篇")

            # 将断点信息存储到实例变量中，供process_all_papers使用
            generator._checkpoint_processed_papers = processed_papers  # type: ignore
            generator._checkpoint_failed_papers = failed_papers  # type: ignore

            # 恢复计数器
            processing_stats: Dict[str, Any] = checkpoint_data.get('processing_stats') or {}
            generator.processed_count.set(processing_stats.get('processed_success', 0))  # type: ignore
            generator.failed_count.set(processing_stats.get('failed_attempts', 0))  # type: ignore

            return True

        except Exception as e:
            self.logger.error(f"加载断点文件失败: {e}")
            return False


class LiteratureReviewGenerator:
    """文献综述生成器主类"""
    
    logger: CustomLogger
    
    def __init__(self, config_file: str = 'config.ini', project_name: Optional[str] = None, pdf_folder: Optional[str] = None):
        self.config_file: str = config_file
        self.project_name: Optional[str] = project_name
        self.pdf_folder: Optional[str] = pdf_folder
        self.config: Optional['ConfigDict'] = None
        self.output_dir: Optional[str] = None
        self.summary_file: Optional[str] = None
        self.papers: List[PaperInfo] = []
        self.summaries: SummariesList = []
        self.failed_papers: List[FailedPaper] = []
        self.processed_count: Counter = Counter(0)
        self.failed_count: Counter = Counter(0)
        self.save_lock: threading.Lock = threading.Lock()

        # 身份基断点续传相关变量
        self._checkpoint_processed_papers: Set[str] = set()
        self._checkpoint_failed_papers: Set[str] = set()

        # 概念增强模式相关变量
        self.concept_mode: bool = False
        self.concept_profile: Optional[Dict[str, Any]] = None

        # 根据参数确定运行模式
        if pdf_folder:
            self.mode: str = "direct"  # 直接PDF模式
            self.pdf_folder = os.path.abspath(pdf_folder)
        else:
            self.mode: str = "zotero"  # Zotero模式（默认）

        # 初始化日志记录器
        self._init_logger()

        # 初始化服务组件
        self.reporting_service: ReportingService = ReportingService(self.logger)
        self.checkpoint_manager: CheckpointManager = CheckpointManager(self.logger)
    
    def _init_logger(self):
        """初始化日志记录器"""
        import logging
        from datetime import datetime
        
        # 创建日志记录器
        self.logger = logging.getLogger(f"llm_reviewer_generator_{datetime.now().strftime('%Y%m%d_%H%M%S')}")  # type: ignore
        self.logger.setLevel(logging.INFO)
        
        # 如果记录器已经有处理器，先清除
        if self.logger.handlers:
            self.logger.handlers.clear()
        
        # 创建控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        
        # 创建格式器
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', 
                                    datefmt='%H:%M:%S')
        console_handler.setFormatter(formatter)
        
        # 添加处理器到记录器
        self.logger.addHandler(console_handler)
    
    def load_configuration(self) -> bool:
        """加载配置文件"""
        try:
            self.config = load_config(self.config_file)
            if not self.config:
                self.logger.error("配置文件加载失败或为空")
                return False
            self.logger.success("配置文件加载成功")
            return True
        except Exception as e:
            self.logger.error(f"配置文件加载异常: {e}")
            return False
    
    def setup_output_directory(self) -> bool:
        """设置输出目录"""
        try:
            # 检查配置是否已加载
            if not self.config:
                self.logger.error("配置未加载，无法设置输出目录")
                return False
            
            # 确定项目名称
            if not self.project_name:
                if self.mode == "zotero":
                    # Zotero模式使用默认项目名
                    self.project_name = "literature_review"
                else:
                    # 直接PDF模式使用文件夹名作为项目名
                    self.project_name = os.path.basename((self.pdf_folder or '').rstrip('/\\'))
            
            # 清理项目名称，移除非法字符
            self.project_name = sanitize_path_component(self.project_name)
            
            # 确定输出路径
            paths_config: Dict[str, str] = self.config.get('Paths', {}) if self.config else {}
            output_base_path: str = paths_config.get('output_path', './output')
            self.output_dir = os.path.join(output_base_path, self.project_name)
            
            # 确保输出目录存在
            if ensure_dir(self.output_dir):
                self.logger.success(f"输出目录已创建: {self.output_dir}")
            else:
                self.logger.error(f"无法创建输出目录: {self.output_dir}")
                return False
            
            # 确定摘要文件路径
            self.summary_file = os.path.join(self.output_dir, f'{self.project_name}_summaries.json')
            
            return True
        except Exception as e:
            self.logger.error(f"设置输出目录失败: {e}")
            return False
    
    def scan_pdf_folder(self) -> bool:
        """扫描PDF文件夹（直接模式专用）"""
        try:
            if self.mode != "direct":
                self.logger.error("scan_pdf_folder只能在直接PDF模式下调用")
                return False
            
            if not self.pdf_folder or not os.path.exists(self.pdf_folder):
                self.logger.error(f"PDF文件夹不存在: {self.pdf_folder}")
                return False
            
            self.logger.info(f"正在扫描PDF文件夹: {self.pdf_folder}")
            
            # 查找所有PDF文件
            pdf_files: List[str] = []
            for root, _dirs, files in os.walk(self.pdf_folder):
                for file in files:
                    if file.lower().endswith('.pdf'):
                        pdf_files.append(os.path.join(root, file))
            
            self.logger.info(f"找到 {len(pdf_files)} 个PDF文件")
            
            # 为每个PDF文件创建论文信息
            self.papers: List[PaperInfo] = []
            for i, pdf_path in enumerate(pdf_files):
                # 从文件名提取标题（移除.pdf扩展名）
                title = os.path.splitext(os.path.basename(pdf_path))[0]
                
                # 尝试从PDF文件中提取额外信息
                pdf_info: Optional[Dict[str, str]] = None  # 明确指定字典值的类型
                try:
                    from pdf_extractor import get_pdf_info  # type: ignore
                    pdf_info = get_pdf_info(pdf_path)
                except Exception as e:
                    self.logger.warning(f"无法从PDF文件提取元数据: {pdf_path}, 错误: {e}")
                
                # 创建论文信息字典
                paper_info: PaperInfo = {
                    'title': title,
                    'authors': [],  # 初始化为空列表
                    'year': '未知年份',  # 年份通常需要OCR才能从PDF中提取，暂时设为默认值
                    'journal': '未知期刊',  # 期刊信息通常需要OCR才能从PDF中提取，暂时设为默认值
                    'doi': '',  # 直接模式下DOI为空
                    'pdf_path': pdf_path,  # PDF文件路径
                    'file_index': i  # 文件索引
                }
                
                # 从PDF信息中提取作者
                if pdf_info:
                    author_str = pdf_info.get('author', '')
                    if author_str and author_str.strip():
                        # 将作者字符串转换为列表格式
                        paper_info['authors'] = [author_str.strip()]
                
                # 如果PDF信息为空或无作者，设为空数组
                authors = paper_info.get('authors', [])
                if not authors:
                    paper_info['authors'] = []
                elif any(author.strip() in ['Unknown', '未知'] for author in authors):
                    paper_info['authors'] = []
                
                # 尝试从文件名中提取年份（简单模式匹配）
                import re
                year_match = re.search(r'(20\d{2})', title)  # 搜索2020-2099年份
                if year_match:
                    paper_info['year'] = year_match.group(1)
                
                # 尝试从文件名中提取作者（如果文件名格式包含下划线分隔的作者名）
                if '_' in title:
                    # 假设文件名格式为: "标题_作者.pdf" 或 "标题_作者_其他信息.pdf"
                    parts = title.split('_')
                    if len(parts) >= 2:
                        potential_author = parts[-1].strip()
                        if potential_author and potential_author != '侯甜甜' and potential_author != '贺爱忠' and potential_author != '周冲' and potential_author != '盘城' and potential_author != '张赛楠' and potential_author != '彭丽徽' and potential_author != '康超' and potential_author != '刘伟华' and potential_author != '朱华东':
                            paper_info['authors'] = [potential_author]
                
                self.papers.append(paper_info)
            
            self.logger.success(f"PDF文件夹扫描完成，共 {len(self.papers)} 篇论文")
            return True
            
        except Exception as e:
            self.logger.error(f"扫描PDF文件夹失败: {e}")
            return False
    
    def parse_zotero_report(self, override_path: Optional[str] = None) -> bool:
        """解析Zotero报告（Zotero模式专用）"""
        try:
            if self.mode != "zotero":
                self.logger.error("parse_zotero_report只能在Zotero模式下调用")
                return False
            
            # 确定Zotero报告路径
            if override_path:
                zotero_report_path = override_path
            else:
                paths_config: Dict[str, str] = self.config.get('Paths', {}) if self.config else {}
                zotero_report_path: str = paths_config.get('zotero_report', '')
            
            if not zotero_report_path or not os.path.exists(zotero_report_path):
                self.logger.error(f"Zotero报告文件不存在: {zotero_report_path}")
                return False
            
            self.logger.info(f"正在解析Zotero报告: {zotero_report_path}")
            
            # 解析报告
            self.papers = parse_zotero_report(zotero_report_path)
            
            if not self.papers:
                self.logger.error("Zotero报告解析失败或报告为空")
                return False
            
            self.logger.success(f"Zotero报告解析完成，共 {len(self.papers)} 篇论文")
            return True
            
        except Exception as e:
            self.logger.error(f"解析Zotero报告失败: {e}")
            return False
    
    @staticmethod
    def get_paper_key(paper: 'Dict[str, Any] | PaperInfo') -> str:
        """为论文生成唯一身份标识"""
        # 优先使用DOI作为唯一标识
        doi = paper.get('doi', '').strip()
        if doi and doi.lower() != 'unknown' and doi.lower() != 'n/a':
            # DOI标准化处理：提取纯粹的ID部分
            import re
            # 匹配DOI ID模式：以10.开头，后跟数字和斜杠
            doi_pattern = r'(10\.\d+/.+)'
            match = re.search(doi_pattern, doi)
            
            if match:
                # 返回标准化的DOI ID部分
                return match.group(1)
            else:
                # 如果无法提取标准格式，返回原始DOI（但进行基本清理）
                # 移除常见的DOI前缀
                doi_clean = re.sub(r'^https?://(doi\.org|dx\.doi\.org)/', '', doi, flags=re.IGNORECASE)
                return doi_clean
        
        # 如果没有DOI，使用标题+作者组合
        title = paper.get('title', '').strip()
        authors = paper.get('authors', [])
        
        # 清理和标准化标题
        if title:
            import re
            title_clean = re.sub(r'[^\w\s]', '', title.lower())
            title_clean = re.sub(r'\s+', ' ', title_clean).strip()
        else:
            title_clean = 'unknown_title'
        
        # 处理作者列表
        if authors and isinstance(authors, list):
            author_surnames: List[str] = []
            for author in authors[:3]:  # 只取前3个作者 # type: ignore
                if isinstance(author, str):
                    name_parts: List[str] = author.strip().split()
                    if name_parts:
                        surname: str = name_parts[-1].lower()
                        author_surnames.append(surname)
            
            if len(authors) > 3:  # type: ignore

                author_surnames.append('et_al')
            
            authors_str = '_'.join(author_surnames) if author_surnames else 'unknown_author'
        else:
            authors_str = 'unknown_author'
        
        # 组合标题和作者作为唯一标识
        return f"{title_clean}_{authors_str}"
    
    def load_existing_summaries(self) -> bool:
        """加载现有摘要文件（用于断点续传）"""
        try:
            if not self.summary_file or not os.path.exists(self.summary_file):
                self.logger.info("未找到现有摘要文件，将开始全新处理")
                self.summaries = []
                return True
            
            with open(self.summary_file, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                self.summaries = loaded_data if isinstance(loaded_data, list) else []
            
            # 验证数据格式
            if not isinstance(self.summaries, list):  # type: ignore
                self.logger.warning("现有摘要文件格式不正确，将开始全新处理")
                self.summaries = []
                return True
            
            success_count = len([s for s in self.summaries if s.get('status') == 'success'])
            failed_count = len([s for s in self.summaries if s.get('status') == 'failed'])
            
            self.logger.success(f"已加载现有摘要文件: {success_count}成功, {failed_count}失败")
            return True
            
        except Exception as e:
            self.logger.warning(f"加载现有摘要文件失败，将开始全新处理: {e}")
            self.summaries = []
            return True  # 即使加载失败也返回True，因为我们仍可以继续处理
    
    def reset_counters(self):
        """重置计数器"""
        self.processed_count.set(0)
        self.failed_count.set(0)
    
    def process_paper(self, paper: PaperInfo, paper_index: int, file_index: Optional[FileIndex], total_papers: int) -> Optional[ProcessingResult]:
        """处理单篇论文"""
        try:
            paper_key = LiteratureReviewGenerator.get_paper_key(paper)  # type: ignore
            
            # 检查是否已在断点中处理过
            if paper_key in self._checkpoint_processed_papers:
                self.logger.info(f"跳过已处理论文: {paper.get('title', '未知标题')}")
                # 从现有摘要中找到对应的条目
                for summary in self.summaries:
                    if summary.get('status') == 'success' and LiteratureReviewGenerator.get_paper_key(summary.get('paper_info', {})) == paper_key:
                        return summary
                return None
            
            if paper_key in self._checkpoint_failed_papers:
                self.logger.info(f"跳过已失败论文: {paper.get('title', '未知标题')}")
                # 从现有摘要中找到对应的条目
                for summary in self.summaries:
                    if summary.get('status') == 'failed' and LiteratureReviewGenerator.get_paper_key(summary.get('paper_info', {})) == paper_key:
                        return summary
                return None
            
            self.logger.info(f"[{paper_index+1}/{total_papers}] 正在处理: {paper.get('title', '未知标题')}")
            
            # 获取PDF文件路径
            pdf_path = paper.get('pdf_path')
            if not pdf_path and self.mode == "zotero":
                # Zotero模式下查找PDF文件
                file_title = paper.get('title', '')
                _file_authors = paper.get('authors', [])
                paths_config: Dict[str, str] = self.config.get('Paths', {}) if self.config else {}
                library_path: str = paths_config.get('library_path', '')
                
                if not library_path:
                    failure_reason = "配置文件中缺少library_path路径"
                    self.logger.error(failure_reason)
                    return {
                        'paper_info': paper,
                        'status': 'failed',
                        'failure_reason': failure_reason
                    }
                
                # 创建文件索引（如果还没有）
                if not file_index:
                    file_index = create_file_index(library_path)
                
                # 使用 file_finder.py 中强大的 find_pdf 函数
                find_result = find_pdf(dict(paper), library_path, file_index)
                
                if find_result and find_result[0]:
                    pdf_path = find_result[0]
                    self.logger.info(f"智能查找到PDF: {os.path.basename(pdf_path)}")
                else:
                    failure_reason: str = find_result[1] if find_result and len(find_result) > 1 else "未找到PDF文件"
                    self.logger.error(f"未找到PDF文件: {file_title} - 原因: {failure_reason}")
                    return {
                        'paper_info': paper,
                        'status': 'failed',
                        'failure_reason': failure_reason
                    }
            elif not pdf_path and self.mode == "direct":
                # 直接模式下PDF路径应该已经存在
                pdf_path = paper.get('pdf_path', '')
            
            if not pdf_path or not os.path.exists(pdf_path):
                failure_reason = f"PDF文件不存在: {pdf_path}"
                self.logger.error(failure_reason)
                return {
                    'paper_info': paper,
                    'status': 'failed',
                    'failure_reason': failure_reason
                }
            
            # 提取PDF文本

            self.logger.info(f"正在提取PDF文本: {os.path.basename(pdf_path)}")

            pdf_text = extract_text_from_pdf(pdf_path)  # type: ignore

            

            if not pdf_text or len(pdf_text.strip()) < 500:  # type: ignore

                failure_reason = f"PDF文本提取失败或内容过少({len(pdf_text) if pdf_text else 0}字符)"  # type: ignore

                self.logger.error(failure_reason)

                return {

                    'paper_info': paper,

                    'status': 'failed',

                    'failure_reason': failure_reason

                }

            

            self.logger.success(f"PDF文本提取成功: {len(pdf_text)}字符")  # type: ignore
            
            # 调用AI API生成摘要
            self.logger.info("正在调用AI生成摘要...")
            
            # 提取分析引擎API配置
            primary_reader_config: Dict[str, str] = self.config.get('Primary_Reader_API', {}) if self.config else {}
            reader_api_config: APIConfig = {
                'api_key': primary_reader_config.get('api_key', ''),
                'model': primary_reader_config.get('model', ''),
                'api_base': primary_reader_config.get('api_base', 'https://api.openai.com/v1')
            }
            
            # 提取备用引擎API配置（用于超长论文）
            backup_reader_config: Dict[str, str] = self.config.get('Backup_Reader_API', {}) if self.config else {}
            backup_api_config: APIConfig = {
                'api_key': backup_reader_config.get('api_key', ''),
                'model': backup_reader_config.get('model', ''),
                'api_base': backup_reader_config.get('api_base', 'https://api.openai.com/v1')
            }
            
            # 构建完整的分析提示词
            try:
                with open('prompts/prompt_analyze.txt', 'r', encoding='utf-8') as f:
                    prompt_template = f.read()
                
                # 替换占位符
                analysis_prompt = prompt_template.replace('{{PAPER_FULL_TEXT}}', pdf_text)
                
            except Exception as e:
                self.logger.warning(f"无法加载分析提示词模板，使用简化提示词: {e}")
                # 简化提示词
                analysis_prompt = f"请分析以下论文内容，生成结构化摘要：\n\n{pdf_text}"
            
            # 调用AI接口生成摘要（自动处理引擎切换）
            ai_result = get_summary_from_ai(analysis_prompt, reader_api_config, backup_api_config, logger=self.logger, config=self.config)
            
            if not ai_result:
                failure_reason = "AI摘要生成失败"
                self.logger.error(failure_reason)
                return {
                    'paper_info': paper,
                    'status': 'failed',
                    'failure_reason': failure_reason
                }
            
            self.logger.success("AI摘要生成成功")
            
            # =================== CONTENT QUALITY CHECK ===================
            # 检查AI生成内容的质量，如果质量不达标则标记为失败
            quality_issues: List[str] = []
            
            # 确保ai_result有基本结构
            if not ai_result or 'common_core' not in ai_result:
                quality_issues.append("AI结果结构不完整")
            else:
                common_core: Dict[str, Any] = ai_result['common_core']  # type: ignore
                # 检查关键字段的内容质量
                key_fields: List[str] = ['methodology', 'findings', 'conclusions']
                empty_fields: List[str] = []
                
                for field in key_fields:
                    value: Any = common_core.get(field, '')
                    # 检查是否为空、null或"未提及"等无效值
                    if not value or value in [None, 'null', 'NULL', '未提及', '未提供', '']:
                        empty_fields.append(field)
                
                # 如果有2个或以上关键字段为空，则判定为质量不达标
                if len(empty_fields) >= 2:
                    quality_issues.append(f"关键字段内容缺失: {', '.join(empty_fields)}")
            
            # 如果发现质量问题，标记为失败
            if quality_issues:
                failure_reason = f"内容质量检查失败: {'; '.join(quality_issues)}"
                self.logger.warning(failure_reason)
                
                # 返回失败结果，触发重试机制
                failed_result: ProcessingResult = {
                    'paper_info': paper,
                    'status': 'failed',
                    'failure_reason': failure_reason
                }
                return failed_result
            
            self.logger.info("内容质量检查通过")
            # ================================================================
            
            # =================== STAGE 1 VALIDATION (MODULAR) ===================
            if self.config and hasattr(self.config, 'getboolean') and self.config.getboolean('Performance', 'enable_stage1_validation', fallback=False):
                ai_result = validator.validate_paper_analysis(self, pdf_text, ai_result)  # type: ignore
            # ===================================================================
            
            # 概念增强分析（如果启用）
            if self.concept_mode and self.concept_profile and ai_result:
                self.logger.info(f"正在对 '{paper.get('title', '未知标题')}' 进行概念增强分析...")
                
                # 读取概念分析提示词模板
                try:
                    with open('prompts/prompt_concept_analysis.txt', 'r', encoding='utf-8') as f:
                        concept_prompt_template = f.read()
                    self.logger.success(f"加载概念分析提示词模板: {len(concept_prompt_template)}字符")
                except Exception as e:
                    self.logger.warning(f"无法加载概念分析提示词模板，使用默认提示词: {e}")
                    concept_prompt_template = "基于提供的背景概念信息和论文摘要，分析该论文在背景概念发展中的作用。\n\n【背景概念】\n{{CONCEPT_PROFILE}}\n\n【论文摘要】\n{{PAPER_SUMMARY}}"
                
                # 准备概念分析的提示词
                concept_prompt = concept_prompt_template.replace(
                    '{{CONCEPT_PROFILE}}', json.dumps(self.concept_profile, ensure_ascii=False)
                ).replace(
                    '{{PAPER_SUMMARY}}', json.dumps(ai_result, ensure_ascii=False)
                )
                
                # 获取写作引擎的 API 配置
                writer_config: Dict[str, str] = self.config.get('Writer_API', {}) if self.config else {}
                writer_api_config: APIConfig = {
                    'api_key': writer_config.get('api_key') or '',  # type: ignore
                    'model': writer_config.get('model') or '',  # type: ignore
                    'api_base': writer_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
                }

                # 调用概念分析接口
                concept_analysis_result = get_concept_analysis(concept_prompt, writer_api_config, logger=self.logger, config=self.config)
                
                if concept_analysis_result:
                    # 将概念分析结果合并到最终的摘要中
                    ai_result['concept_analysis'] = concept_analysis_result
                    self.logger.success("概念增强分析成功。")
                else:
                    self.logger.warning("概念增强分析失败。")

            # =================== METADATA BACKFILL ===================
            # AI提取的元数据回填到paper_info中，解决Direct PDF Mode下的元数据显示问题
            try:
                if ai_result and 'common_core' in ai_result:
                    common_core = ai_result['common_core']
                    
                    # 提取AI分析出的元数据
                    extracted_title = common_core.get('title', '').strip()
                    extracted_authors = common_core.get('authors', [])
                    extracted_year = common_core.get('year', '').strip()
                    extracted_journal = common_core.get('journal', '').strip()
                    extracted_doi = common_core.get('doi', '').strip()
                    
                    # 验证提取的元数据是否有效（非空且不是"未知"等占位符）
                    valid_title = extracted_title and extracted_title not in ['', '未知', 'N/A', '无标题']
                    valid_year = extracted_year and extracted_year not in ['', '未知', 'N/A', '未知年份']
                    valid_journal = extracted_journal and extracted_journal not in ['', '未知', 'N/A', '未知期刊']
                    
                    # 更新paper_info中的元数据字段
                    if valid_title:
                        paper['title'] = extracted_title
                    
                    # 处理authors字段：可能是字符串或列表
                    if extracted_authors:
                        if isinstance(extracted_authors, list):
                            # 如果是列表，直接使用
                            if extracted_authors:  # 确保列表不为空
                                paper['authors'] = extracted_authors
                        elif isinstance(extracted_authors, str):
                            # 如果是字符串，尝试分割为列表
                            authors_str = extracted_authors.strip()
                            if authors_str and authors_str not in ['', '未知', 'N/A']:
                                # 尝试按常见分隔符分割
                                import re
                                authors_list = re.split(r'[,，、;；和and]\s*', authors_str)
                                authors_list = [author.strip() for author in authors_list if author.strip()]
                                if authors_list:
                                    paper['authors'] = authors_list
                    
                    # 更新年份和期刊信息
                    if valid_year:
                        paper['year'] = extracted_year
                    
                    if valid_journal:
                        paper['journal'] = extracted_journal
                    
                    # 更新DOI（如果有的话）
                    if extracted_doi:
                        paper['doi'] = extracted_doi
                    
                    # 记录元数据更新情况
                    updated_fields: List[str] = []
                    if valid_title:
                        updated_fields.append('标题')
                    if extracted_authors:
                        updated_fields.append('作者')
                    if valid_year:
                        updated_fields.append('年份')
                    if valid_journal:
                        updated_fields.append('期刊')
                    if extracted_doi:
                        updated_fields.append('DOI')
                    
                    if updated_fields:
                        self.logger.info(f"✅ 元数据回填成功，更新字段: {', '.join(updated_fields)}")
                    else:
                        self.logger.info("ℹ️  未发现有效的AI提取元数据，使用默认值")
                        
            except Exception as e:
                self.logger.warning(f"元数据回填失败: {e}")
            # =============================================================

            # 构造结果
            result: ProcessingResult = {
                'paper_info': paper,
                'status': 'success',
                'ai_summary': ai_result,  # type: ignore
                'processing_time': datetime.now().isoformat(),
                'text_length': len(pdf_text) if pdf_text else 0  # type: ignore
            }
            
            return result
            
        except Exception as e:
            failure_reason = f"处理论文时发生异常: {str(e)}"
            self.logger.error(failure_reason)
            traceback.print_exc()
            failed_result: ProcessingResult = {
                'paper_info': paper,
                'status': 'failed',
                'failure_reason': failure_reason
            }
            return failed_result
    
    def save_summaries(self) -> bool:
        """保存摘要到JSON文件（线程安全版本）"""
        try:
            if not self.output_dir or not self.summary_file:
                self.logger.error("输出目录或摘要文件路径未设置")
                return False
            
            # 确保输出目录存在
            if not ensure_dir(self.output_dir):
                self.logger.error(f"无法创建输出目录: {self.output_dir}")
                return False
            
            # 创建备份文件（如果原文件存在）
            if os.path.exists(self.summary_file):
                backup_file = f"{self.summary_file}.backup"
                try:
                    import shutil
                    shutil.copy2(self.summary_file, backup_file)
                    self.logger.debug(f"已更新摘要文件备份: {backup_file}")
                except Exception as e:
                    self.logger.debug(f"无法更新摘要文件备份: {e}")
            
            # 使用线程锁确保线程安全（如果存在）
            if hasattr(self, 'save_lock'):
                with self.save_lock:
                    # 原子性写入文件
                    temp_file = f"{self.summary_file}.tmp"
                    
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        # 写入数据
                        json.dump(self.summaries, f, ensure_ascii=False, indent=2)
                        f.flush()
                    
                    # 原子性重命名到目标文件
                    os.replace(temp_file, self.summary_file)
            else:
                # 无锁版本（向后兼容）
                # 原子性写入文件
                temp_file = f"{self.summary_file}.tmp"
                
                with open(temp_file, 'w', encoding='utf-8') as f:
                    # 写入数据
                    json.dump(self.summaries, f, ensure_ascii=False, indent=2)
                    f.flush()
                
                # 原子性重命名到目标文件
                os.replace(temp_file, self.summary_file)
            
            self.logger.debug(f"[保存] 摘要文件已更新: {len(self.summaries)}条记录")
            return True
            
        except Exception as e:
            self.logger.error(f"保存摘要文件失败: {e}")
            self.logger.error(f"摘要列表类型: {type(self.summaries)}")
            self.logger.error(f"摘要列表长度: {len(self.summaries)}")
            
            # 尝试保存错误报告
            try:
                error_file = f"{self.summary_file}.error" if self.summary_file else None
                if error_file:
                    with open(error_file, 'w', encoding='utf-8') as f:
                        json.dump({
                            'error': str(e),
                            'timestamp': datetime.now().isoformat(),
                            'summaries_count': len(self.summaries),
                            'summaries_type': str(type(self.summaries))
                        }, f, ensure_ascii=False, indent=2)
            except:
                pass
            return False
    
    def generate_excel_report(self) -> bool:
        """生成Excel报告"""
        try:
            if not self.output_dir or not self.project_name:
                return False
            
            excel_file = os.path.join(self.output_dir, f'{self.project_name}_analyzed_papers.xlsx')
            
            # 生成Excel报告
            success = generate_excel_report(self)
            
            if success:
                self.logger.success(f"Excel报告已生成: {excel_file}")
                return True
            else:
                self.logger.error("Excel报告生成失败")
                return False
                
        except Exception as e:
            self.logger.error(f"生成Excel报告失败: {e}")
            return False
    
    def generate_failure_report(self) -> bool:
        """生成失败报告"""
        try:
            if not self.output_dir or not self.project_name:
                return False
            
            failure_report_file = os.path.join(self.output_dir, f'{self.project_name}_failed_papers_report.txt')
            
            # 生成失败报告
            success = generate_failure_report(self)
            
            if success:
                self.logger.success(f"失败报告已生成: {failure_report_file}")
                return True
            else:
                self.logger.error("失败报告生成失败")
                return False
                
        except Exception as e:
            self.logger.error(f"生成失败报告失败: {e}")
            return False
    
    def generate_retry_zotero_report(self) -> bool:
        """生成Zotero重跑报告（仅Zotero模式）"""
        try:
            if self.mode != "zotero":
                return True  # 直接模式下不需要生成重跑报告
            
            if not self.output_dir or not self.project_name:
                return False
            
            # 类型守卫：确保output_dir和project_name不是None
            assert self.output_dir is not None and self.project_name is not None
            
            retry_report_file = os.path.join(self.output_dir, f'{self.project_name}_zotero_report_for_retry.txt')
            
            # 生成重跑报告
            success = generate_retry_zotero_report(self)
            
            if success:
                self.logger.success(f"重跑报告已生成: {retry_report_file}")
                return True
            else:
                self.logger.error("重跑报告生成失败")
                return False
                
        except Exception as e:
            self.logger.error(f"生成重跑报告失败: {e}")
            return False
    
    def process_all_papers(self) -> bool:
        """处理所有论文（并发处理版本）"""
        try:
            if not self.papers:
                self.logger.error("没有论文需要处理")
                return False
            
            total_papers = len(self.papers)
            self.logger.info(f"开始并发处理 {total_papers} 篇论文")
            
            # 确定最大工作线程数
            performance_config: Dict[str, str] = self.config.get('Performance', {}) if self.config else {}
            max_workers = int(performance_config.get('max_workers', 3))
            self.logger.info(f"使用 {max_workers} 个工作线程")
            
            # 创建文件索引（Zotero模式）
            file_index: Optional[FileIndex] = None
            if self.mode == "zotero":
                paths_config: Dict[str, str] = self.config.get('Paths', {}) if self.config else {}
                library_path: str = paths_config.get('library_path', '')
                if library_path:
                    self.logger.info("正在创建文件索引...")
                    file_index = create_file_index(library_path)
                    self.logger.success(f"文件索引创建完成，包含 {len(file_index)} 个文件")
            
            # 确定需要处理的论文（跳过已处理的）
            papers_to_process: List[Tuple[int, 'PaperInfo']] = []
            skipped_count = 0
            
            for i, paper in enumerate(self.papers):
                paper_key = LiteratureReviewGenerator.get_paper_key(paper)  # type: ignore
                if paper_key in self._checkpoint_processed_papers or paper_key in self._checkpoint_failed_papers:
                    skipped_count += 1
                    continue
                papers_to_process.append((i, paper))
            
            self.logger.info(f"需要处理: {len(papers_to_process)}篇论文，跳过: {skipped_count}篇论文")
            
            if not papers_to_process:
                self.logger.success("所有论文都已处理完成")
                return True
            
            # 重置计数器
            self.reset_counters()
            
            # 创建进度条
            progress_bar = tqdm(total=len(papers_to_process), desc="[阶段一] 正在分析文献")
            
            # 创建线程池并提交任务
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # 提交所有任务
                future_to_paper: Dict[concurrent.futures.Future['ProcessingResult | None'], Tuple[int, 'PaperInfo']] = {
                    executor.submit(self.process_paper, paper, i, file_index, total_papers): (i, paper)
                    for i, paper in papers_to_process
                }
                
                # 处理完成的任务
                for future in concurrent.futures.as_completed(future_to_paper):
                    _, paper = future_to_paper[future]
                    paper_key = LiteratureReviewGenerator.get_paper_key(paper)  # type: ignore
                    
                    try:
                        result = future.result()
                        
                        if result and result.get('status') == 'success':
                            # 处理成功
                            with self.save_lock:
                                self.summaries.append(result)
                                self._checkpoint_processed_papers.add(paper_key)
                            
                            # 线程安全地增加计数器
                            with self.save_lock:
                                self.processed_count.increment()
                            
                            # 更新进度条
                            progress_bar.update(1)
                            # 更新进度条的后缀信息
                            progress_bar.set_postfix_str(f"成功: {self.processed_count.value}, 失败: {self.failed_count.value}")
                        else:
                            # 处理失败
                            failure_reason = result.get('failure_reason') or '未知错误' if result else '处理返回空结果'
                            if not isinstance(failure_reason, str):  # type: ignore
                                failure_reason = '未知错误'
                            failed_paper = result.get('paper_info', paper) if result else paper
                            
                            self.failed_papers.append({  # type: ignore
                                    'paper_info': failed_paper,
                                    'failure_reason': failure_reason
                                })
                                # 更新身份基断点跟踪
                            self._checkpoint_failed_papers.add(paper_key)
                            
                            # 线程安全地增加计数器
                            with self.save_lock:
                                self.failed_count.increment()
                            
                            # 更新进度条
                            progress_bar.update(1)
                            # 更新进度条的后缀信息
                            progress_bar.set_postfix_str(f"成功: {self.processed_count.value}, 失败: {self.failed_count.value}")
                        
                        # 每完成一个任务就立即保存数据，确保数据不丢失
                        if result and result.get('status') == 'success':
                            save_result = self.save_summaries()
                            if not save_result:
                                self.logger.error("⚠️ 警告: 数据保存失败，请检查磁盘空间和权限")
                        else:
                            # 失败的情况下定期保存（每3个失败保存一次）
                            if (self.processed_count.get_value() + self.failed_count.get_value()) % 3 == 0:
                                self.save_summaries()
                                self.save_checkpoint()
                        
                    except Exception as e:
                        # 任务执行异常
                        failure_reason = f"处理过程发生异常: {str(e)}"
                        
                        with self.save_lock:
                            self.failed_papers.append({  # type: ignore
                                'paper_info': paper,
                                'failure_reason': failure_reason
                            })
                            # 更新身份基断点跟踪
                            self._checkpoint_failed_papers.add(paper_key)
                        
                        # 线程安全地增加计数器
                        with self.save_lock:
                            self.failed_count.increment()
                        
                        self.logger.error(f"任务执行异常: {e}")
                        self.logger.error(f"失败: {self.processed_count.value}成功, {self.failed_count.value}失败 - {failure_reason}")
                        
                        # 异常情况下立即保存，确保数据不丢失
                        save_result = self.save_summaries()
                        if not save_result:
                            self.logger.error("⚠️ 警告: 异常情况下数据保存失败")
                        self.save_checkpoint()
            
            # 最终保存所有数据
            self.save_summaries()
            self.save_checkpoint()
            
            self.logger.success("\n并发处理完成！")
            self.logger.info(f"总文献数: {total_papers}")
            self.logger.info(f"本次处理: {len(papers_to_process)}篇")
            self.logger.info(f"跳过已处理: {skipped_count}篇")
            self.logger.info(f"成功处理: {self.processed_count.value}")
            self.logger.info(f"失败: {self.failed_count.value}")
            self.logger.info(f"摘要文件: {self.summary_file}")
            
            # 自动重试循环 - 第一阶段末尾
            if self.failed_papers:
                self.logger.warning(f"有{len(self.failed_papers)}篇论文处理失败，启动自动重试循环...")
                
                # 定义可重试的失败类型关键词
                retriable_keywords = ['api', 'network', 'http', 'timeout', '500', '502', '503', '504', '429', '连接', '超时', '错误', '失败']
                
                # 分离可重试和永久失败的论文
                retriable_failures: List['FailedPaper'] = []
                permanent_failures: List['FailedPaper'] = []
                
                for failed_item in self.failed_papers:
                    failure_reason: str = failed_item.get('failure_reason', '').lower()
                    paper_info: Dict[str, Any] = failed_item.get('paper_info', {})  # type: ignore
                    
                    # 检查失败原因是否包含可重试关键词
                    is_retriable = any(keyword in failure_reason for keyword in retriable_keywords)
                    
                    if is_retriable:
                        retriable_failures.append(failed_item)
                    else:
                        permanent_failures.append(failed_item)
                
                self.logger.info(f"可重试失败论文: {len(retriable_failures)}篇")
                self.logger.info(f"永久失败论文: {len(permanent_failures)}篇")
                
                # 执行最多2轮重试
                max_retry_rounds = 2
                for retry_round in range(1, max_retry_rounds + 1):
                    if not retriable_failures:
                        self.logger.info("没有可重试的失败论文，结束重试循环")
                        break
                    
                    self.logger.info(f"正在对 {len(retriable_failures)} 篇失败文献进行第 {retry_round} 轮自动重试...")
                    
                    # 准备重试论文数据
                    retry_papers: List[Tuple[int, Dict[str, Any]]] = []
                    retry_indices: List[int] = []
                    for failed_item in retriable_failures:
                        paper_info: Dict[str, Any] = failed_item.get('paper_info', {})  # type: ignore
                        # 找到原始论文索引

                        for i, original_paper in enumerate(self.papers):

                            if LiteratureReviewGenerator.get_paper_key(original_paper) == LiteratureReviewGenerator.get_paper_key(paper_info):

                                retry_papers.append((i, original_paper))  # type: ignore  # 使用original_paper而不是paper_info

                                retry_indices.append(i)

                                break
                    
                    if not retry_papers:
                        self.logger.warning("无法找到重试论文的原始索引，结束重试")
                        break
                    
                    # 重置当前轮次的失败列表
                    current_round_failures: List[Dict[str, Any]] = []
                    
                    # 创建线程池进行重试处理
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as retry_executor:
                        retry_futures: Dict[concurrent.futures.Future['ProcessingResult | None'], Tuple[Dict[str, Any], int]] = {}
                        for original_index, paper in retry_papers:
                            future = retry_executor.submit(self.process_paper, paper, original_index, file_index, total_papers)  # type: ignore
                            retry_futures[future] = (paper, original_index)
                        
                        # 处理重试结果
                        retry_progress_bar = tqdm(concurrent.futures.as_completed(retry_futures), 
                                                total=len(retry_papers), desc=f"[重试第{retry_round}轮] 正在重试文献")
                        
                        for future in retry_progress_bar:
                            paper, original_index = retry_futures[future]
                            paper_key = LiteratureReviewGenerator.get_paper_key(paper)  # type: ignore
                            
                            try:
                                result = future.result()
                                if result and result.get('status') == 'success':
                                    # 重试成功，添加到结果列表
                                    with self.save_lock:
                                        self.summaries.append(result)
                                        self._checkpoint_processed_papers.add(paper_key)
                                        # 从失败列表中移除
                                        self.failed_papers = [fp for fp in self.failed_papers  # type: ignore
                                                          if LiteratureReviewGenerator.get_paper_key(fp.get('paper_info', {})) != paper_key]
                                    
                                    with self.save_lock:
                                        self.processed_count.increment()
                                    
                                    retry_progress_bar.update(1)
                                    retry_progress_bar.set_postfix_str(f"成功: {self.processed_count.value}, 失败: {self.failed_count.value}")
                                else:
                                    # 重试仍然失败
                                    failure_reason = result.get('failure_reason', '重试失败') if result else '重试返回空结果'
                                    current_round_failures.append({
                                        'paper_info': paper,
                                        'failure_reason': failure_reason
                                    })
                                    
                                    retry_progress_bar.update(1)
                                    retry_progress_bar.set_postfix_str(f"成功: {self.processed_count.value}, 失败: {self.failed_count.value}")
                                
                                # 重试成功时立即保存，确保数据不丢失
                                if result and result.get('status') == 'success':
                                    save_result = self.save_summaries()
                                    if not save_result:
                                        self.logger.error("⚠️ 警告: 重试成功数据保存失败")
                                else:
                                    # 失败情况下定期保存
                                    if (self.processed_count.get_value() + self.failed_count.get_value()) % 3 == 0:
                                        self.save_summaries()
                                        self.save_checkpoint()
                                
                            except Exception as e:
                                # 重试异常
                                failure_reason = f"重试过程发生异常: {str(e)}"
                                current_round_failures.append({
                                    'paper_info': paper,
                                    'failure_reason': failure_reason
                                })
                                
                                self.logger.error(f"重试任务执行异常: {e}")
                                # 重试异常时立即保存，确保数据不丢失
                                save_result = self.save_summaries()
                                if not save_result:
                                    self.logger.error("⚠️ 警告: 重试异常时数据保存失败")
                                self.save_checkpoint()
                    
                    # 更新重试失败列表
                    retriable_failures = current_round_failures  # type: ignore
                    
                    if current_round_failures:
                        self.logger.warning(f"第 {retry_round} 轮重试后，仍有 {len(current_round_failures)} 篇论文失败")
                    else:
                        self.logger.success(f"第 {retry_round} 轮重试成功，所有论文处理完成！")
                        break
                
                # 合并最终失败列表
                final_failed_papers = permanent_failures + retriable_failures
                self.failed_papers = final_failed_papers  # type: ignore
                
                # 更新失败计数
                self.failed_count.set(len(self.failed_papers))
                
                self.logger.info(f"自动重试循环完成！最终失败论文数: {len(self.failed_papers)}篇")
            
            # 生成失败报告
            if self.failed_papers:
                self.logger.warning(f"有{len(self.failed_papers)}篇论文处理失败，将生成失败报告")
            
            # 最终保存所有数据
            self.save_summaries()
            self.save_checkpoint()
            
            return True
            
        except KeyboardInterrupt:
            self.logger.error("\n\n用户中断处理")
            self.logger.info(f"已处理: {self.processed_count.value}篇文献，失败: {self.failed_count.value}篇")
            self.save_summaries()
            self.save_checkpoint()
            return False
        except Exception as e:
            self.logger.error(f"并发处理过程中出错: {e}")
            self.logger.info(f"已处理: {self.processed_count.value}篇文献，失败: {self.failed_count.value}篇")
            self.save_summaries()
            self.save_checkpoint()
            return False

    def save_checkpoint(self) -> bool:
        """保存基于身份的断点文件 - 委托给CheckpointManager"""
        return self.checkpoint_manager.save_checkpoint(self)

    def load_checkpoint(self) -> bool:
        """加载基于身份的断点文件 - 委托给CheckpointManager"""
        return self.checkpoint_manager.load_checkpoint(self)

    def run_stage_one(self, override_zotero_report_path: Optional[str] = None) -> bool:
        """阶段一：文献解析与AI摘要生成（基于身份的断点续传版本）"""
        self.logger.info("=" * 60 + "\n文献综述自动生成器 - 阶段一（身份基断点续传）\n" + "=" * 60)
        try:
            # 加载配置文件
            if not self.load_configuration(): 
                return False
            # 确保配置已正确加载到实例变量
            if not self.config:
                self.logger.error("配置未正确加载")
                return False
            
            # 如果提供了重写的Zotero报告路径，在此处应用
            if override_zotero_report_path:
                self.logger.info(f"[重跑模式] 已将文献来源强制指定为 -> {override_zotero_report_path}")
            
            if not self.setup_output_directory(): 
                return False
            
            # 加载基于身份的断点文件
            checkpoint_loaded = self.load_checkpoint()
            if not checkpoint_loaded:
                self.logger.info("[全新开始] 未找到有效断点，将开始全新处理")
                self.reset_counters()
                # 初始化断点跟踪变量
                self._checkpoint_processed_papers = set()
                self._checkpoint_failed_papers = set()
            else:
                self.logger.info("[断点续传] 已加载处理进度，将跳过已处理的论文")
            
            # 加载现有摘要（兼容旧版本）
            self.load_existing_summaries()
            
            # 逻辑分叉：根据运行模式选择数据源
            if self.mode == "zotero":
                # Zotero模式：解析Zotero报告，传递覆盖路径
                if not self.parse_zotero_report(override_zotero_report_path): 
                    return False
            else:
                # 直接模式：扫描PDF文件夹
                if not self.scan_pdf_folder(): 
                    return False
            
            # 验证论文数据完整性
            if not self.papers:
                self.logger.error("未找到任何论文数据")
                return False
            
            self.logger.info(f"论文数据加载完成: {len(self.papers)}篇论文")
            
            # 处理所有论文（使用身份基断点续传）
            success = self.process_all_papers()
            
            # 如果处理成功，生成报告
            if success:
                # 清除断点文件（表示全部完成）
                if self.output_dir and self.project_name:
                    # 类型守卫：确保output_dir和project_name不是None
                    assert self.output_dir is not None and self.project_name is not None
                    checkpoint_file = os.path.join(self.output_dir, f'{self.project_name}_checkpoint.json')
                    if os.path.exists(checkpoint_file):
                        try:
                            os.remove(checkpoint_file)
                            self.logger.info("已清除断点文件，所有论文处理完成")
                        except Exception as e:
                            self.logger.warning(f"无法清除断点文件: {e}")
                
                # 调用统一的报告生成方法
                self.generate_all_reports()
            
            return success
            
        except Exception as e:
            self.logger.error(f"阶段一运行失败: {e}")
            # 即使失败也要保存断点
            self.save_checkpoint()
            return False

    
    



    def generate_all_reports(self) -> None:
        """生成所有分析阶段的报告 - 委托给ReportingService"""
        self.reporting_service.generate_all_reports(self)
    
    def extract_section_title_from_outline(self, outline_content: str, section_number: int) -> Optional[str]:
        """从大纲内容中提取指定章节的标题"""
        try:
            lines = outline_content.split('\n')
            current_section = 0
            
            for line in lines:
                # 查找二级标题（##）
                if line.startswith('## '):
                    current_section += 1
                    if current_section == section_number:
                        return line[3:].strip()
            
            return None
        except Exception as e:
            self.logger.error(f"提取章节标题失败: {e}")
            return None

    def create_literature_review_section(self, section_number: int, section_title: str, outline_content: str) -> bool:
        """创建文献综述的指定章节内容"""
        try:
            section_content = self.generate_review_section_content(section_title, outline_content)
            if not section_content:
                self.logger.error(f"第{section_number}章内容生成失败")
                return False
            
            # section_content应该是纯文本字符串
            if not isinstance(section_content, str):  # type: ignore
                self.logger.warning("预期收到纯文本，但收到其他格式，正在转换...")
                section_text = str(section_content)
            else:
                section_text = section_content
            
            # 生成Word文档路径（添加项目名称前缀）
            if not self.output_dir:
                self.logger.error("输出目录未设置")
                return False
                
            if self.project_name:
                word_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review.docx')
            else:
                word_file = os.path.join(self.output_dir, 'literature_review.docx')
            
            # 将章节内容追加到Word文档
            success = self.append_section_to_word_document(section_number, section_title, section_text, word_file)
            
            if success:
                self.logger.success(f"第{section_number}章已追加到文献综述: {word_file}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"创建文献综述章节失败: {e}")
            return False

    def generate_review_section_content(self, section_title: str, outline_content: str) -> Optional[str]:
        """生成指定章节的内容（带智能续写循环）"""
        try:
            # 将整个summaries列表转换为格式化的JSON字符串（包含两段式结构）
            summaries_string = json.dumps(self.summaries, ensure_ascii=False, indent=2)
            self.logger.success(f"生成摘要JSON字符串: {len(summaries_string)}字符")

            # 将完整的大纲内容作为字符串
            outline_string = outline_content
            self.logger.success(f"生成大纲字符串: {len(outline_string)}字符")

            # 提取写作引擎API配置
            writer_config: Dict[str, Any] = (self.config or {}).get('Writer_API', {})  # type: ignore
            writer_api_config: APIConfig = {
                'api_key': writer_config.get('api_key') or '',  # type: ignore
                'model': writer_config.get('model') or '',  # type: ignore
                'api_base': writer_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
            }

            self.logger.info(f"正在调用写作引擎生成章节内容: {section_title}")

            # 智能续写循环实现
            partial_section_content = ""  # 存储已生成的章节内容
            continuation_attempts = 0  # 续写计数器
            max_continuation_attempts = 5  # 最大续写次数（安全熔断）

            while continuation_attempts <= max_continuation_attempts:
                if continuation_attempts == 0:
                    # 首次调用，使用原始章节提示词
                    self.logger.info(f"[章节生成] 首次调用生成章节: {section_title}")
                    result = self._call_section_api(
                        section_title, 
                        summaries_string, 
                        outline_string, 
                        writer_api_config, 
                        is_continuation=False
                    )
                else:
                    # 续写调用，使用续写提示词
                    self.logger.info(f"[章节续写] 第{continuation_attempts}次续写: {section_title}")
                    result = self._call_section_api(
                        section_title, 
                        summaries_string, 
                        outline_string, 
                        writer_api_config, 
                        is_continuation=True,
                        partial_content=partial_section_content
                    )

                if not result:
                    self.logger.error(f"[章节生成] API调用失败，章节生成中断")
                    return None

                # 解析返回结果

                section_content = result.get('content', '')  # type: ignore

                finish_reason = result.get('finish_reason', 'stop')  # type: ignore

                if not section_content or len(section_content.strip()) < 100:
                    self.logger.warning(f"[章节生成] 返回内容过短({len(section_content)}字符)，重试...")
                    continuation_attempts += 1
                    continue

                # 将新内容追加到已生成内容中
                if continuation_attempts == 0:
                    partial_section_content = section_content
                else:
                    partial_section_content += section_content

                self.logger.success(f"[章节生成] 本次生成 {len(section_content)} 字符，累计 {len(partial_section_content)} 字符")

                # 检查是否需要继续续写
                if finish_reason == 'stop':
                    self.logger.success(f"[章节生成] 章节生成完成，无需续写")
                    return partial_section_content
                elif finish_reason == 'length':
                    self.logger.info(f"[章节生成] 内容被截断，准备续写...")
                    continuation_attempts += 1
                    if continuation_attempts > max_continuation_attempts:
                        self.logger.warning(f"[章节生成] 达到最大续写次数({max_continuation_attempts})，返回部分生成的内容")
                        return partial_section_content
                else:
                    self.logger.warning(f"[章节生成] 未知的finish_reason: {finish_reason}，假设完成")
                    return partial_section_content

            # 达到最大续写次数
            self.logger.warning(f"[章节生成] 达到最大续写次数({max_continuation_attempts})，返回部分生成的内容")
            return partial_section_content

        except Exception as e:
            self.logger.error(f"生成章节内容失败: {e}")
            return None

    def _call_section_api(self, section_title: str, summaries_string: str, outline_string: str, 
                         writer_api_config: 'APIConfig', is_continuation: bool = False, 
                         partial_content: str = "") -> Optional[Dict[str, Any]]:
        """调用章节生成API的私有方法"""
        try:
            # Determine system prompt
            try:
                with open('prompts/prompt_system_section.txt', 'r', encoding='utf-8') as f:
                    system_prompt = f.read()
                self.logger.success(f"加载章节系统提示词模板: {len(system_prompt)}字符")
            except Exception as e:
                self.logger.warning(f"无法加载章节系统提示词模板，使用默认提示词: {e}")
                system_prompt = """你是一个学术文献综述专家。请基于提供的文献分析结果和完整大纲，撰写指定章节的正文内容。

要求：
1. 直接输出纯文本格式的章节正文内容
2. 不要包含章节标题
3. 内容需要专业、客观、全面
4. 适当引用具体文献以支持论点
5. 语言风格需专业、学术
6. 只撰写指定章节的内容，不要包含其他章节"""

            # Determine final prompt
            if is_continuation:
                try:
                    with open('prompts/prompt_continue_section.txt', 'r', encoding='utf-8') as f:
                        section_prompt_template = f.read()
                    self.logger.success(f"加载章节续写提示词模板: {len(section_prompt_template)}字符")
                except Exception as e:
                    self.logger.warning(f"无法加载章节续写提示词模板，使用默认提示词: {e}")
                    section_prompt_template = "【角色】你是一位正在撰写综述特定章节的学者，刚才思路被打断了。\n【任务】请你继续完成一份未写完的章节正文。\n\n【全部论文分析数据】\n{{SUMMARIES_JSON_ARRAY}}\n\n【综述完整大纲】\n{{REVIEW_OUTLINE}}\n\n【当前需要撰写的章节标题】\n{{SECTION_TITLE}}\n\n【已完成的章节草稿】\n{{PARTIAL_SECTION_CONTENT}}"

                final_prompt = section_prompt_template.replace('{{SUMMARIES_JSON_ARRAY}}', summaries_string)
                final_prompt = final_prompt.replace('{{REVIEW_OUTLINE}}', outline_string)
                final_prompt = final_prompt.replace('{{SECTION_TITLE}}', section_title)
                final_prompt = final_prompt.replace('{{PARTIAL_SECTION_CONTENT}}', partial_content)
            else:
                try:
                    with open('prompts/prompt_synthesize_section.txt', 'r', encoding='utf-8') as f:
                        section_prompt_template = f.read()
                    self.logger.success(f"加载章节提示词模板: {len(section_prompt_template)}字符")
                except Exception as e:
                    self.logger.warning(f"无法加载章节提示词模板，使用默认提示词: {e}")
                    section_prompt_template = "基于以下文献摘要信息和大纲，请撰写指定章节的内容。\n\n【全部论文分析数据】\n{{SUMMARIES_JSON_ARRAY}}\n\n【综述完整大纲】\n{{REVIEW_OUTLINE}}\n\n【当前需要撰写的章节标题】\n{{SECTION_TITLE}}"

                final_prompt = section_prompt_template.replace('{{SUMMARIES_JSON_ARRAY}}', summaries_string)
                final_prompt = final_prompt.replace('{{REVIEW_OUTLINE}}', outline_string)
                final_prompt = final_prompt.replace('{{SECTION_TITLE}}', section_title)

            self.logger.success(f"生成最终章节提示词: {len(final_prompt)}字符")

            # Call unified AI API function
            ai_response = _call_ai_api(
                prompt=final_prompt,
                api_config=writer_api_config,
                system_prompt=system_prompt,
                max_tokens=6000,
                temperature=0.7,
                response_format="text" # Expecting plain text
            )

            if ai_response:
                # _call_ai_api returns content directly for text format
                return {
                    'content': ai_response,
                    'finish_reason': 'stop' # _call_ai_api doesn't return finish_reason for text, assume stop
                }
            else:
                self.logger.error(f"章节内容生成失败: _call_ai_api 返回空值")
                return None

        except Exception as e:
            self.logger.error(f"调用章节API失败: {e}")
            return None

    def append_section_to_word_document(self, section_number: int, section_title: str, section_text: str, word_file: str) -> bool:
        """将章节内容追加到Word文档（带样式配置）"""
        return append_section_to_word_document(self, section_number, section_title, section_text, word_file)

    def generate_full_review_from_outline(self) -> bool:
        """从大纲生成完整文献综述"""
        self.logger.info("=" * 60 + "\n文献综述自动生成器 - 阶段二：综述生成\n" + "=" * 60)
        try:
            if not self.load_configuration(): 
                return False
            if not self.setup_output_directory(): 
                return False
            if not self.load_existing_summaries():
                self.logger.error("无法加载摘要文件，请先运行阶段一")
                return False
            if not self.summaries:
                self.logger.error("没有找到任何摘要，请先运行阶段一")
                return False
            
            writer_config: Dict[str, Any] = (self.config or {}).get('Writer_API', {})  # type: ignore
            if 'dummy' in (writer_config.get('api_key') or ''):  # type: ignore
                if not self.output_dir:
                    self.logger.error("输出目录未设置")
                    return False
                    
                if self.project_name:
                    word_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review.docx')
                else:
                    word_file = os.path.join(self.output_dir, 'literature_review.docx')
                doc = Document()  # type: ignore
                doc.add_heading('Dummy Literature Review', 0)
                doc.add_paragraph('This is a dummy literature review.')
                doc.save(word_file)
                self.logger.success(f"Dummy review saved to {word_file}")
                return True
            
            # 加载大纲文件
            if not self.output_dir:
                self.logger.error("输出目录未设置")
                return False
                
            if self.project_name:
                outline_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review_outline.md')  # type: ignore
                review_checkpoint_file = os.path.join(self.output_dir, f'{self.project_name}_review_checkpoint.json')
                word_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review.docx')
            else:
                outline_file = os.path.join(self.output_dir, 'literature_review_outline.md')
                review_checkpoint_file = os.path.join(self.output_dir, 'review_checkpoint.json')
                word_file = os.path.join(self.output_dir, 'literature_review.docx')
            
            if not os.path.exists(outline_file):
                self.logger.error(f"大纲文件不存在: {outline_file}，请先运行 --generate-outline 生成大纲")
                return False
            
            with open(outline_file, 'r', encoding='utf-8') as f:
                outline_content = f.read()
            
            # 解析大纲中的所有章节
            import re
            section_matches = re.findall(r"^##\s*(\d+)\.\s*(.*)", outline_content, re.MULTILINE)
            
            if not section_matches:
                self.logger.error("大纲中没有找到任何章节（格式：## 数字. 标题）")
                return False
            
            self.logger.info(f"从大纲中解析到 {len(section_matches)} 个章节")
            
            # 验证章节编号连续性
            section_numbers = [int(match[0]) for match in section_matches]
            section_numbers.sort()
            for i in range(1, len(section_numbers)):
                if section_numbers[i] != section_numbers[i-1] + 1:
                    self.logger.error(f"大纲章节编号不连续：发现第{section_numbers[i-1]}章后直接是第{section_numbers[i]}章")
                    self.logger.error("请检查大纲文件，确保章节编号连续（如1, 2, 3...）")
                    return False
            self.logger.success("大纲章节编号验证通过：编号连续")
            
            # 检查断点续传文件
            last_completed_section = 0
            if os.path.exists(review_checkpoint_file):
                try:
                    with open(review_checkpoint_file, 'r', encoding='utf-8') as f:
                        checkpoint = json.load(f)
                        last_completed_section = checkpoint.get('last_completed_section', 0)
                    
                    if last_completed_section > 0:
                        self.logger.info(f"[断点续传] 发现综述生成断点，将从第 {last_completed_section + 1} 章开始继续...")
                    else:
                        self.logger.info("[全新开始] 未发现有效断点，将从第1章开始生成")
                except Exception as e:
                    self.logger.warning(f"读取断点文件失败，将从头开始: {e}")
                    last_completed_section = 0
            else:
                self.logger.info("[全新开始] 未发现断点文件，将从第1章开始生成")
            
            # 洁净启动机制：全新任务时删除旧文件
            if last_completed_section == 0 and os.path.exists(word_file):
                self.logger.info(f"检测到已存在的旧综述文件，将创建全新版本: {word_file}")
                try:
                    os.remove(word_file)
                except Exception as e:
                    self.logger.error(f"无法删除旧的综述文件，请检查文件权限: {e}")
                    return False
            
            # 创建或加载Word文档
            doc = None
            if os.path.exists(word_file) and last_completed_section > 0:
                # 断点续传：加载现有文档
                try:
                    doc = Document(word_file)  # type: ignore
                    self.logger.info(f"[断点续传] 已加载现有文档: {word_file}")
                except Exception as e:
                    self.logger.error(f"加载现有文档失败，将创建新文档: {e}")
                    doc = Document()  # type: ignore
            else:
                # 全新开始：创建新文档
                doc = Document()  # type: ignore
                
                # 加载样式配置
                style_config = self.config.get('Styling') if self.config else {}  # type: ignore
                font_name = style_config.get('font_name', 'Times New Roman')  # type: ignore
                font_size_body = int(style_config.get('font_size_body', '12'))  # type: ignore
                font_size_heading1 = int(style_config.get('font_size_heading1', '16'))  # type: ignore
                font_size_heading2 = int(style_config.get('font_size_heading2', '14'))  # type: ignore
                
                # 设置默认字体
                doc.styles['Normal'].font.name = font_name  # type: ignore
                doc.styles['Normal'].font.size = Pt(font_size_body)  # type: ignore
                
                # 设置中文字体
                doc.styles['Normal']._element  # type: ignore.rPr.rFonts.set(qn('w:eastAsia'), font_name)  # type: ignore
                
                # 设置标题样式
                doc.styles['Heading 1'].font.name = font_name  # type: ignore
                doc.styles['Heading 1'].font.size = Pt(font_size_heading1)  # type: ignore
                doc.styles['Heading 1']._element  # type: ignore.rPr.rFonts.set(qn('w:eastAsia'), font_name)  # type: ignore
                
                doc.styles['Heading 2'].font.name = font_name  # type: ignore
                doc.styles['Heading 2'].font.size = Pt(font_size_heading2)  # type: ignore
                doc.styles['Heading 2']._element  # type: ignore.rPr.rFonts.set(qn('w:eastAsia'), font_name)  # type: ignore
                
                title = doc.add_heading('文献综述', level=0)
                if title is not None:  # type: ignore
                    title.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER  # type: ignore

                # 应用标题样式
                for run in title.runs:

                    run.font.name = font_name  # type: ignore

                    run.font.size = Pt(font_size_heading1 + 2)  # 主标题稍大  # type: ignore
                
                # 添加生成时间
                date_para = doc.add_paragraph(f"生成时间: {datetime.now().strftime('%Y年%m月%d日')}")
                date_para.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER  # type: ignore
                
                # 应用日期样式

                for run in date_para.runs:

                    run.font.name = font_name  # type: ignore

                    run.font.size = Pt(font_size_body)  # type: ignore
            
            # 用tqdm包装章节列表，显示进度条
            progress_bar = tqdm(enumerate(section_matches, 1), total=len(section_matches), desc="[阶段二] 正在生成综述章节")
            
            # 逐章生成内容（从断点开始）
            for i, (section_num, section_title) in progress_bar:
                # 跳过已完成的章节
                if i <= last_completed_section:
                    self.logger.info(f"[跳过] 第{section_num}章已完成，继续下一章...")
                    continue
                
                # 新增：跳过参考文献和附录章节
                if "参考文献" in section_title or "附录" in section_title:
                    self.logger.info(f"[跳过] 第{section_num}章 '{section_title}' 将在最后由程序自动生成。")
                    continue
                
                # 更新进度条的当前章节信息
                progress_bar.set_postfix_str(f"当前章节: {section_num}. {section_title[:30]}...")
                
                self.logger.info(f"正在生成第{section_num}章: {section_title}")
                
                # 生成章节内容
                section_content = self.generate_review_section_content(section_title, outline_content)
                if not section_content:
                    self.logger.error(f"第{section_num}章内容生成失败")
                    continue
                
                # 添加章节标题和内容到Word文档
                doc.add_paragraph()  # 空行分隔
                
                heading = doc.add_heading(f'第{section_num}章 {section_title}', level=1)
                heading.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT  # type: ignore
                
                # 加载样式配置
                style_config = self.config.get('Styling') if self.config else {}  # type: ignore
                font_name = style_config.get('font_name', 'Times New Roman')  # type: ignore
                font_size_body = int(style_config.get('font_size_body', '12'))  # type: ignore
                font_size_heading1 = int(style_config.get('font_size_heading1', '16'))  # type: ignore
                
                # 应用标题样式
                for run in heading.runs:

                    run.font.name = font_name  # type: ignore

                    run.font.size = Pt(font_size_heading1)  # type: ignore
                
                # 将章节内容按段落分割并添加到文档
                paragraphs = section_content.split('\n\n')
                for para in paragraphs:
                    para = para.strip()
                    if para:
                        p = doc.add_paragraph(para)
                        # 应用正文字体样式

                        for run in p.runs:

                            run.font.name = font_name  # type: ignore

                            run.font.size = Pt(font_size_body)  # type: ignore
                
                # 更新断点文件（每完成一章就更新断点，但不立即保存文档）
                checkpoint_data: Dict[str, Any] = {  # type: ignore
                    'last_completed_section': i,
                    'last_section_title': section_title,
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                with open(review_checkpoint_file, 'w', encoding='utf-8') as f:
                    json.dump(checkpoint_data, f, ensure_ascii=False, indent=2)
                
                self.logger.success(f"第{section_num}章已处理并更新断点")
            
            # 在所有章节处理完成后，一次性保存文档
            doc.save(word_file)
            self.logger.success(f"完整文献综述已保存: {word_file}")
            
            # 生成APA参考文献（总是执行，确保参考文献总是存在）
            self.logger.info("正在生成APA参考文献...")
            references = self.generate_apa_references()
            if references:
                doc.add_paragraph()  # 空行分隔
                ref_heading = doc.add_heading('参考文献', level=1)
                ref_heading.alignment = WD_PARAGRAPH_ALIGNMENT.LEFT  # type: ignore
                
                # 加载样式配置
                style_config = self.config.get('Styling') if self.config else {}  # type: ignore
                font_name = style_config.get('font_name', 'Times New Roman')  # type: ignore
                font_size_body = int(style_config.get('font_size_body', '12'))  # type: ignore
                
                # 应用参考文献标题样式
                for run in ref_heading.runs:

                    run.font.name = font_name  # type: ignore
                
                # 添加参考文献，应用APA悬挂缩进
                for ref in references:
                    p = doc.add_paragraph(ref)
                    # 应用正文字体样式

                    for run in p.runs:

                        run.font.name = font_name  # type: ignore

                        run.font.size = Pt(font_size_body)  # type: ignore
                    # 设置APA悬挂缩进：首行不缩进，后续行缩进1.27厘米（0.5英寸）
                    p.paragraph_format.first_line_indent = 0
                    p.paragraph_format.left_indent = Pt(36)  # type: ignore
                
                self.logger.success(f"已添加 {len(references)} 条参考文献（APA格式）")
            else:
                self.logger.warning("未生成任何参考文献，请检查摘要数据是否完整")
            
            # 最终保存
            doc.save(word_file)
            
            # 清除断点文件（表示全部完成）
            if os.path.exists(review_checkpoint_file):
                os.remove(review_checkpoint_file)
                self.logger.info("已清除断点文件，所有章节生成完成")
            
            # 生成目录（在最终保存前）
            if last_completed_section < len(section_matches) or not os.path.exists(review_checkpoint_file):
                self.logger.info("正在生成Word文档目录...")
                self.generate_word_table_of_contents(doc)  # type: ignore
                self.logger.success("目录已生成")
                
                # 最终保存
                doc.save(word_file)
            
            self.logger.success(f"完整文献综述已生成: {word_file}")
            
            # 第二阶段验证（根据配置决定是否自动运行）
            try:
                if self.config and self.config.getboolean('Performance', 'enable_stage2_validation', fallback=False):
                    self.logger.info("根据配置文件自动启动第二阶段验证...")
                    from validator import run_review_validation
                    validation_success = run_review_validation(self)
                    if validation_success:
                        self.logger.success("第二阶段验证完成！验证报告已生成。")
                    else:
                        self.logger.warning("第二阶段验证失败，请检查验证报告文件。")
                else:
                    self.logger.info("第二阶段验证未在配置中启用。如需运行验证，请使用: --validate-review")
            except Exception as e:
                self.logger.error(f"第二阶段验证运行时出错: {e}")
                self.logger.info("您可以手动运行验证命令: python main.py --validate-review")
            
            return True
            
        except Exception as e:
            self.logger.error(f"从大纲生成文献综述失败: {e}")
            return False

    def generate_word_table_of_contents(self, doc: Any) -> bool:  # type: ignore
        """为Word文档生成自动目录"""
        return generate_word_table_of_contents(doc)

    def generate_apa_references(self) -> List[str]:
        """生成APA格式的参考文献列表"""
        return generate_apa_references(self)

    

    def generate_literature_review_outline(self) -> bool:
        """生成文献综述大纲（带智能续写循环）"""
        self.logger.info("=" * 60 + "\n文献综述自动生成器 - 阶段二：大纲生成\n" + "=" * 60)
        try:
            if not self.load_configuration(): 
                return False
            if not self.setup_output_directory(): 
                return False
            if not self.load_existing_summaries():
                self.logger.error("无法加载摘要文件，请先运行阶段一")
                return False
            if not self.summaries:
                self.logger.error("没有找到任何摘要，请先运行阶段一")
                return False
            
            writer_config: Dict[str, Any] = (self.config or {}).get('Writer_API', {})  # type: ignore
            if 'dummy' in (writer_config.get('api_key') or ''):  # type: ignore
                outline_content = "# Dummy Outline\n\n## Introduction\n\n## Body Paragraph\n\n## Conclusion"
                if self.project_name:
                    outline_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review_outline.md')  # type: ignore
                else:
                    outline_file = os.path.join(self.output_dir, 'literature_review_outline.md')  # type: ignore
                with open(outline_file, 'w', encoding='utf-8') as f:  # type: ignore
                    f.write(outline_content)
                self.logger.success(f"Dummy outline saved to {outline_file}")
                return True
            
            self.logger.info(f"已加载{len(self.summaries)}个文献摘要")
            return self.create_literature_review_outline()
        except Exception as e:
            self.logger.error(f"阶段二运行失败: {e}")
            return False

    def create_literature_review_outline(self) -> bool:
        """创建文献综述大纲，适配新的纯文本输出格式"""
        try:
            review_data = self.prepare_review_data()
            outline_content = self.generate_review_outline(review_data)
            if not outline_content:
                self.logger.error("文献综述大纲生成失败")
                return False
            
            # outline_content应该是纯文本字符串
            if not isinstance(outline_content, str):  # type: ignore
                self.logger.warning("预期收到纯文本，但收到其他格式，正在转换...")
                outline_text = str(outline_content)
            else:
                outline_text = outline_content
            
            # 生成大纲文件路径（添加项目名称前缀）
            if self.project_name:
                outline_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review_outline.md')  # type: ignore
            else:
                outline_file = os.path.join(self.output_dir, 'literature_review_outline.md')  # type: ignore
            
            # 保存大纲文件
            with open(outline_file, 'w', encoding='utf-8') as f:  # type: ignore
                f.write(outline_text)
            
            self.logger.success(f"文献综述大纲已生成: {outline_file}")
            # 根据模式提供不同的命令提示
            if self.mode == "direct":
                self.logger.info("大纲已生成。请检查并修改。然后，运行以下命令生成完整综述：")
                self.logger.info(f"命令: python main.py --pdf-folder \"{self.pdf_folder}\" --generate-review")
            else:
                self.logger.info("大纲已生成。请检查并修改。然后，运行以下命令生成完整综述：")
                self.logger.info(f"命令: python main.py --project-name \"{self.project_name}\" --generate-review")
            return True
                
        except Exception as e:
            self.logger.error(f"创建文献综述大纲失败: {e}")
            return False

    def generate_review_outline(self, review_data: Dict[str, Any]) -> Optional[str]:
        """生成综述大纲内容，适配新的两段式JSON输入（智能续写循环版本）"""
        try:
            # 从提示词模板文件读取大纲提示词
            outline_prompt_template = ""
            try:
                with open('prompts/prompt_synthesize_outline.txt', 'r', encoding='utf-8') as f:
                    outline_prompt_template = f.read()
                self.logger.success(f"加载大纲提示词模板: {len(outline_prompt_template)}字符")
            except Exception as e:
                self.logger.warning(f"无法加载大纲提示词模板，使用默认提示词: {e}")
                try:
                    with open('prompts/prompt_default_outline.txt', 'r', encoding='utf-8') as f:
                        outline_prompt_template = f.read()
                    self.logger.success(f"加载默认大纲提示词模板: {len(outline_prompt_template)}字符")
                except Exception as e2:
                    self.logger.error(f"无法加载默认大纲提示词模板: {e2}")
                    outline_prompt_template = "基于以下文献摘要信息，请生成一份详细的文献综述大纲。\n\n{{SUMMARIES_JSON_ARRAY}}"

            # 从提示词模板文件读取续写提示词
            continue_prompt_template = ""
            try:
                with open('prompts/prompt_continue_outline.txt', 'r', encoding='utf-8') as f:
                    continue_prompt_template = f.read()
                self.logger.success(f"加载续写提示词模板: {len(continue_prompt_template)}字符")
            except Exception as e:
                self.logger.warning(f"无法加载续写提示词模板，使用默认提示词: {e}")
                try:
                    with open('prompts/prompt_default_continue_outline.txt', 'r', encoding='utf-8') as f:
                        continue_prompt_template = f.read()
                    self.logger.success(f"加载默认续写提示词模板: {len(continue_prompt_template)}字符")
                except Exception as e2:
                    self.logger.error(f"无法加载默认续写提示词模板: {e2}")
                    continue_prompt_template = "请继续完成这份未写完的文献综述大纲。\n\n【全部论文分析数据】\n{{SUMMARIES_JSON_ARRAY}}\n\n【已完成的大纲草稿】\n{{PARTIAL_OUTLINE}}"

            # 将整个summaries列表转换为格式化的JSON字符串（包含两段式结构）
            summaries_string = json.dumps(self.summaries, ensure_ascii=False, indent=2)
            self.logger.success(f"生成摘要JSON字符串: {len(summaries_string)}字符")

            # 提取写作引擎API配置
            writer_config: Dict[str, Any] = (self.config or {}).get('Writer_API', {})  # type: ignore
            writer_api_config: APIConfig = {
                'api_key': writer_config.get('api_key') or '',  # type: ignore
                'model': writer_config.get('model') or '',  # type: ignore
                'api_base': writer_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
            }

            self.logger.info("正在调用写作引擎生成文献综述大纲（智能续写循环模式）...")

            # ===== 智能续写循环核心逻辑 =====
            partial_outline = ""  # 存储已生成的大纲内容
            continuation_attempts = 0  # 续写计数器
            max_continuation_attempts = 5  # 最大续写次数（安全熔断机制）
            
            while continuation_attempts <= max_continuation_attempts:
                try:
                    # 根据是否为首次调用选择不同的提示词
                    if continuation_attempts == 0:
                        # 首次调用：使用原始大纲提示词
                        final_prompt = outline_prompt_template.replace('{{SUMMARIES_JSON_ARRAY}}', summaries_string)
                        self.logger.info(f"首次大纲生成，提示词长度: {len(final_prompt)}字符")
                    else:
                        # 续写调用：使用续写提示词
                        final_prompt = continue_prompt_template.replace('{{SUMMARIES_JSON_ARRAY}}', summaries_string)
                        final_prompt = final_prompt.replace('{{PARTIAL_OUTLINE}}', partial_outline)  # type: ignore
                        self.logger.info(f"续写大纲生成(第{continuation_attempts}次)，提示词长度: {len(final_prompt)}字符")  # type: ignore

                    # 调用AI API
                    # 加载系统提示词
                    try:
                        with open('prompts/prompt_system_outline.txt', 'r', encoding='utf-8') as f:
                            system_prompt = f.read()
                        self.logger.success(f"加载大纲系统提示词模板: {len(system_prompt)}字符")
                    except Exception as e:
                        self.logger.warning(f"无法加载大纲系统提示词模板，使用默认提示词: {e}")
                        system_prompt = """你是一个学术文献综述专家。请基于提供的文献分析结果生成一份详细的文献综述大纲。

要求：
1. 直接输出Markdown格式的大纲内容
2. 使用Markdown的标题格式（# 主要标题, ## 章节标题, ### 小节标题）
3. 每个章节标题下，用项目符号（-）列出该章节应包含的核心论点或分析要点
4. 大纲应该结构清晰、逻辑严谨
5. 不要包含任何正文内容，只输出大纲"""

                    ai_response_text = _call_ai_api(
                        prompt=final_prompt,
                        api_config=writer_api_config,
                        system_prompt=system_prompt,
                        max_tokens=8192,
                        temperature=0.7,
                        response_format="text"
                    )
                    
                    if ai_response_text is None:
                        self.logger.error("API调用失败，无法生成大纲")
                        return None
                    
                    # 模拟旧API的返回结构以适配后续逻辑
                    ai_response = {'choices': [{'message': {'content': ai_response_text}, 'finish_reason': 'stop'}]}  # type: ignore
                    
                    # 提取AI回复内容和完成原因
                    outline_content = ai_response['choices'][0]['message']['content']  # type: ignore
                    finish_reason = ai_response['choices'][0]['finish_reason']  # type: ignore
                    
                    if outline_content and len(outline_content) > 100:  # type: ignore
                        # 将本次生成的内容追加到部分大纲中
                        if continuation_attempts == 0:
                            partial_outline = outline_content  # type: ignore
                        else:
                            partial_outline += "\n\n" + outline_content  # type: ignore
                        
                        self.logger.success(f"大纲片段生成成功，当前总长度: {len(partial_outline)}字符")  # type: ignore
                        self.logger.info(f"完成原因: {finish_reason}")
                        
                        # 检查是否需要继续续写
                        if finish_reason == 'stop':  # type: ignore
                            self.logger.success("大纲生成完成，无需续写")
                            return partial_outline  # type: ignore
                        elif finish_reason == 'length':
                            self.logger.info("大纲被截断，准备续写...")
                            continuation_attempts += 1
                            continue
                        else:
                            self.logger.warning(f"未知的完成原因: {finish_reason}，尝试续写...")
                            continuation_attempts += 1
                            continue
                    else:
                        self.logger.warning(f"大纲内容过短({len(outline_content) if outline_content else 0}字符)，重试...")  # type: ignore
                        continuation_attempts += 1
                        continue

                except Exception as e:
                    self.logger.error(f"大纲生成过程出错: {str(e)}")
                    continuation_attempts += 1
                    if continuation_attempts <= max_continuation_attempts:
                        self.logger.info(f"准备重试第{continuation_attempts}次...")
                        continue
                    else:
                        break
            
            # 安全熔断：达到最大续写次数
            if continuation_attempts > max_continuation_attempts:
                self.logger.error(f"[ERROR] 大纲生成续写次数过多({continuation_attempts}次)，或已陷入死循环。请检查输入数据或Prompt。")
                if partial_outline and len(partial_outline) > 100:  # type: ignore  # 只有部分内容足够长才返回
                    self.logger.warning("返回部分生成的大纲内容")
                    return partial_outline  # type: ignore
                self.logger.error("大纲生成失败，内容过短或为空")
                return None
            
            # 最终检查：只有内容足够长才认为成功
            if partial_outline and len(partial_outline) > 100:  # type: ignore
                return partial_outline  # type: ignore
            else:
                self.logger.error("大纲生成失败，内容过短或为空")
                return None

        except Exception as e:
            self.logger.error(f"生成大纲内容失败: {e}")
            return None

    

    def create_literature_review(self) -> bool:
        """创建文献综述，适配新的纯文本输出格式"""
        try:
            review_data = self.prepare_review_data()
            review_content = self.generate_review_content(review_data)
            if not review_content:
                self.logger.error("文献综述生成失败")
                return False
            
            # review_content现在应该是纯文本字符串
            if not isinstance(review_content, str):  # type: ignore
                self.logger.warning("预期收到纯文本，但收到其他格式，正在转换...")
                review_text = str(review_content)
            else:
                review_text = review_content
            
            # 生成Word文档路径（添加项目名称前缀）
            if not self.output_dir:
                self.logger.error("输出目录未设置")
                return False
                
            if self.project_name:
                word_file = os.path.join(self.output_dir, f'{self.project_name}_literature_review.docx')
            else:
                word_file = os.path.join(self.output_dir, 'literature_review.docx')
            
            # 创建Word文档
            success = self.create_word_document(review_text, word_file)
            
            if success:
                self.logger.success(f"文献综述Word文档已生成: {word_file}")
                return True
            else:
                return False
                
        except Exception as e:
            self.logger.error(f"创建文献综述失败: {e}")
            return False

    def prepare_review_data(self) -> Dict[str, Any]:
        review_data: Dict[str, Any] = {  # type: ignore
            'total_papers': len(self.summaries),
            'successful_papers': len([s for s in self.summaries if s.get('status') == 'success']),
            'failed_papers': len([s for s in self.summaries if s.get('status') != 'success']), 
            'papers': [],
            'research_areas': {}, 
            'methodologies': {}, 
            'key_findings': [], 
            'common_themes': []
        }
        
        for summary in self.summaries:
            if summary.get('status') != 'success': 
                continue
                
            paper_info = summary.get('paper_info', {})
            ai_summary: Union[AISummary, Dict[str, Any], None] = summary.get('ai_summary', {})
            
            # 适配新的两段式结构
            if 'common_core' in ai_summary:  # type: ignore
                # 新的两段式结构
                common_core = ai_summary['common_core']  # type: ignore
            else:
                # 兼容旧的单段式结构
                common_core = ai_summary  # type: ignore
            
            paper_data: Dict[str, Any] = {  # type: ignore
                'title': paper_info.get('title', '未知标题'),
                'authors': paper_info.get('authors', []),
                'year': paper_info.get('year', '未知年份'),
                'journal': paper_info.get('journal', '未知期刊'),
                'summary': common_core.get('summary', ''),  # type: ignore
                'key_points': common_core.get('key_points', []),  # type: ignore
                'methodology': common_core.get('methodology', ''),  # type: ignore
                'findings': common_core.get('findings', ''),  # type: ignore
                'conclusions': common_core.get('conclusions', ''),  # type: ignore
                'relevance': common_core.get('relevance', ''),  # type: ignore
                'limitations': common_core.get('limitations', '')  # type: ignore
            }
            
            review_data['papers'].append(paper_data)  # type: ignore
            
            methodology = paper_data['methodology']
            if methodology: 
                review_data['methodologies'][methodology] = review_data['methodologies'].get(methodology, 0) + 1  # type: ignore
                
            findings = paper_data['findings']  # type: ignore
            if findings: 
                review_data['key_findings'].append(findings)  # type: ignore
                
        return review_data

    def generate_review_content(self, review_data: Dict[str, Any]) -> Optional[str]:
        """生成综述内容，适配新的两段式JSON输入"""
        try:
            # 从提示词模板文件读取综述提示词
            synthesize_prompt_template = ""
            try:
                with open('prompts/prompt_synthesize.txt', 'r', encoding='utf-8') as f:
                    synthesize_prompt_template = f.read()
                self.logger.success(f"加载综述提示词模板: {len(synthesize_prompt_template)}字符")
            except Exception as e:
                self.logger.warning(f"无法加载综述提示词模板，使用默认提示词: {e}")
                try:
                    with open('prompts/prompt_default_synthesize.txt', 'r', encoding='utf-8') as f:
                        synthesize_prompt_template = f.read()
                    self.logger.success(f"加载默认综述提示词模板: {len(synthesize_prompt_template)}字符")
                except Exception as e2:
                    self.logger.error(f"无法加载默认综述提示词模板: {e2}")
                    synthesize_prompt_template = "基于以下文献摘要信息，请生成一份完整的文献综述报告。\n\n{{SUMMARIES_JSON_ARRAY}}"

            # 将整个summaries列表转换为格式化的JSON字符串（包含两段式结构）
            summaries_string = json.dumps(self.summaries, ensure_ascii=False, indent=2)
            self.logger.success(f"生成摘要JSON字符串: {len(summaries_string)}字符")

            # 将完整的JSON字符串注入到模板中
            final_prompt = synthesize_prompt_template.replace('{{SUMMARIES_JSON_ARRAY}}', summaries_string)
            self.logger.success(f"生成最终综述提示词: {len(final_prompt)}字符")

            # 提取写作引擎API配置
            writer_config: Dict[str, Any] = (self.config or {}).get('Writer_API', {})  # type: ignore
            writer_api_config: APIConfig = {
                'api_key': writer_config.get('api_key') or '',  # type: ignore
                'model': writer_config.get('model') or '',  # type: ignore
                'api_base': writer_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
            }

            self.logger.info("正在调用写作引擎生成文献综述...")

            # ===== 专门为综述调用设计的API接口（不强制JSON格式）=====
            import requests  # type: ignore

            api_key = writer_api_config.get('api_key') or ''
            api_base = writer_api_config.get('api_base', 'https://api.openai.com/v1')
            model_name = writer_api_config.get('model') or ''

            api_url = f"{api_base.rstrip('/')}/chat/completions"  # type: ignore

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

            # 专门为综述设计的系统提示词（返回纯文本）
            try:
                with open('prompts/prompt_system_synthesize.txt', 'r', encoding='utf-8') as f:
                    system_prompt = f.read()
                self.logger.success(f"加载综述系统提示词模板: {len(system_prompt)}字符")
            except Exception as e:
                self.logger.warning(f"无法加载综述系统提示词模板，使用默认提示词: {e}")
                system_prompt = """你是一个学术文献综述专家。请基于提供的文献分析结果生成一份完整的中文学术综述报告。

要求：
1. 直接输出纯文本格式的综述内容，不要使用JSON格式
2. 使用markdown格式组织结构（标题用#, ##等）
3. 内容需要专业、客观、全面
4. 适当引用具体文献以支持论点
5. 总字数控制在3000-5000字"""

            payload: Dict[str, Any] = {  # type: ignore
                "model": model_name,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": final_prompt
                    }
                ],
                "temperature": 0.7,
                "max_tokens": 8000  # 综述需要更长的响应
            }

            # 重试逻辑
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    self.logger.info(f"综述生成尝试 {attempt + 1}/{max_retries}")

                    response = requests.post(
                        api_url,
                        headers=headers,
                        json=payload,
                        timeout=300  # 5分钟超时
                    )

                    response.raise_for_status()
                    response_data = response.json()

                    # 提取AI回复内容
                    review_content = response_data['choices'][0]['message']['content']

                    if review_content and len(review_content) > 100:
                        self.logger.success("写作引擎返回综述文本")
                        return review_content
                    else:
                        self.logger.warning(f"综述内容过短({len(review_content)}字符)，重试...")

                except requests.exceptions.HTTPError as e:
                    if attempt < max_retries - 1:
                        wait_time = 2 * (2 ** attempt)
                        self.logger.warning(f"HTTP错误 {response.status_code if 'response' in locals() else '?'}，{wait_time:.1f}秒后重试...")  # type: ignore
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"综述生成失败: {str(e)}")
                        return None

                except Exception as e:
                    if attempt < max_retries - 1:
                        wait_time = 2 * (2 ** attempt)
                        self.logger.warning(f"错误: {str(e)}，{wait_time:.1f}秒后重试...")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"综述生成最终失败: {str(e)}")
                        return None

            return None

        except Exception as e:
            self.logger.error(f"生成综述内容失败: {e}")
            return None

    @staticmethod
    def build_review_prompt(review_data: Dict[str, Any]) -> str:
        papers_info = []
        for i, paper in enumerate(review_data['papers'], 1):
            paper_text = f"文献 {i}: {paper['title']}\n作者: {', '.join(paper['authors']) if paper['authors'] else '未知'}\n年份: {paper['year']}\n期刊: {paper['journal']}\n\n摘要: {paper['summary']}\n\n研究方法: {paper['methodology']}\n主要发现: {paper['findings']}\n结论: {paper['conclusions']}\n相关性: {paper['relevance']}\n局限性: {paper['limitations']}\n\n关键要点:\n{chr(10).join(['- ' + point for point in paper['key_points']])}"
            papers_info.append(paper_text)  # type: ignore
        all_papers_text = '\n'.join(papers_info)  # type: ignore
        prompt = f"基于以下{review_data['total_papers']}篇学术文献的摘要信息，请生成一份完整的文献综述报告。\n\n文献信息:\n{all_papers_text}\n\n请按照以下结构生成文献综述：\n\n# 文献综述报告\n\n## 1. 引言\n- 研究领域概述\n- 研究背景和意义\n- 文献综述的目的和范围\n\n## 2. 研究现状分析\n- 主要研究主题和趋势\n- 研究方法的分析和比较\n- 关键发现的总结\n\n## 3. 研究热点和前沿\n- 当前研究的热点问题\n- 新兴的研究方向\n- 尚未解决的问题\n\n## 4. 研究方法和质量分析\n- 常用研究方法的评价\n- 研究质量的总体评估\n- 研究的局限性分析\n\n## 5. 综合讨论\n- 主要共识和分歧\n- 研究的理论贡献\n- 实践意义和应用前景\n\n## 6. 未来研究方向\n- 基于现有研究空白的建议\n- 方法学改进的建议\n- 理论和实践的发展方向\n\n## 7. 结论\n- 主要发现总结\n- 对领域的贡献\n- 综述的局限性\n\n## 参考文献\n- 按照学术规范列出所有文献\n\n要求：\n1. 内容要全面、客观、准确\n2. 要有批判性思维和分析\n3. 要指出研究趋势和未来方向\n4. 语言要专业、简洁、清晰\n5. 总字数在3000-5000字之间"
        return prompt

    @staticmethod
    def format_review_content(review_content: Dict[str, Any], review_data: Dict[str, Any]) -> str:
        header = f"# 文献综述报告\n\n**生成时间**: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n**文献数量**: {review_data['total_papers']}篇\n**成功处理**: {review_data['successful_papers']}篇\n**失败处理**: {review_data['failed_papers']}篇\n\n---\n\n"
        review_text = review_content if isinstance(review_content, str) else review_content.get('summary', json.dumps(  # type: ignore
            review_content, ensure_ascii=False, indent=2))
        references = "\n\n## 参考文献\n\n"
        for i, paper in enumerate(review_data['papers'], 1):
            authors = ', '.join(paper['authors']) if paper['authors'] else '未知作者';
            year = f" ({paper['year']})" if paper['year'] != '未知年份' else '';
            journal = f". {paper['journal']}" if paper['journal'] != '未知期刊' else ''
            references += f"{i}. {authors}{year}. {paper['title']}{journal}.\n"
        return header + review_text + references

    def create_word_document(self, markdown_text: str, output_path: str) -> bool:
        """将Markdown文本解析并创建Word文档（带样式配置）"""
        return create_word_document(self, markdown_text, output_path)

    def run_priming_phase(self, concept_name: str, seed_folder: str) -> bool:
        """概念学习阶段：分析核心论文以建立概念理解"""
        self.logger.info("=" * 60 + "\n概念学习阶段：建立概念理解\n" + "=" * 60)
        try:
            if not self.load_configuration():
                return False
            if not self.setup_output_directory():
                return False
            
            # 验证种子文件夹
            if not os.path.exists(seed_folder):
                self.logger.error(f"种子文件夹不存在: {seed_folder}")
                return False
            
            # 扫描种子论文
            seed_papers = []
            for root, _, files in os.walk(seed_folder):
                for file in files:
                    if file.lower().endswith('.pdf'):
                        seed_papers.append(os.path.join(root, file))  # type: ignore
            
            if not seed_papers:
                self.logger.error(f"种子文件夹中未找到PDF文件: {seed_folder}")
                return False
            
            self.logger.info(f"找到 {len(seed_papers)} 篇种子论文")  # type: ignore

            # 处理种子论文 - 保持完整信息量，使用并发处理
            concept_papers = []

            # 使用并发处理提高速度，但保持完整信息量
            max_workers = min(2, len(seed_papers))  # type: ignore  # 最多2个并发，避免API限制
            
            def process_seed_paper(pdf_path: str) -> Optional[Dict[str, Any]]:  # type: ignore
                """处理单个种子论文"""
                try:
                    self.logger.info(f"正在分析种子论文: {os.path.basename(pdf_path)}")  # type: ignore
                    
                    # 提取完整文本
                    pdf_text = extract_text_from_pdf(pdf_path)  # type: ignore
                    if not pdf_text or len(pdf_text.strip()) < 500:  # type: ignore
                        self.logger.warning(f"种子论文文本提取失败: {os.path.basename(pdf_path)}")  # type: ignore
                        return None
                    
                    # 创建论文信息
                    paper_info: Dict[str, Any] = {
                        'title': os.path.splitext(os.path.basename(pdf_path))[0],
                        'authors': [],
                        'year': '未知年份',
                        'journal': '未知期刊',
                        'doi': '',
                        'pdf_path': pdf_path
                    }  # type: ignore
                    
                    # 获取API配置
                    primary_config: Dict[str, str] = self.config.get('Primary_Reader_API', {}) if self.config else {}  # type: ignore
                    backup_config: Dict[str, str] = self.config.get('Backup_Reader_API', {}) if self.config else {}  # type: ignore
                    
                    reader_api_config: APIConfig = {
                        'api_key': primary_config.get('api_key', ''),  # type: ignore
                        'model': primary_config.get('model', ''),  # type: ignore
                        'api_base': primary_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
                    }
                    
                    backup_api_config: APIConfig = {
                        'api_key': backup_config.get('api_key', ''),  # type: ignore
                        'model': backup_config.get('model', ''),  # type: ignore
                        'api_base': backup_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
                    }
                    
                    # 构建完整的分析提示词
                    try:
                        with open('prompts/prompt_analyze.txt', 'r', encoding='utf-8') as f:
                            prompt_template: str = f.read()  # type: ignore
                        
                        # 替换占位符
                        analysis_prompt: str = prompt_template.replace('{{PAPER_FULL_TEXT}}', pdf_text)  # type: ignore
                        
                    except Exception as e:
                        self.logger.warning(f"无法加载分析提示词模板，使用简化提示词: {e}")
                        # 简化提示词
                        analysis_prompt = f"请分析以下论文内容，生成结构化摘要：\n\n{pdf_text}"
                    
                    # 调用AI分析
                    ai_result = get_summary_from_ai(analysis_prompt, reader_api_config, backup_api_config, logger=self.logger, config=self.config)
                    if ai_result:
                        self.logger.success(f"种子论文分析成功: {os.path.basename(pdf_path)}")
                        return {
                            'paper_info': paper_info,
                            'ai_summary': ai_result
                        }
                    else:
                        self.logger.warning(f"种子论文分析失败: {os.path.basename(pdf_path)}")
                        return None
                        
                except Exception as e:
                    self.logger.error(f"处理种子论文时出错 {os.path.basename(pdf_path)}: {e}")
                    return None
            
            for future in concurrent.futures.as_completed(future_to_pdf):  # type: ignore
                result: Optional[Dict[str, Any]] = future.result()  # type: ignore
                if result:
                    concept_papers.append(result)  # type: ignore
            
            if not concept_papers:
                self.logger.error("没有成功分析任何种子论文")
                return False
            
            # 生成概念配置
            self.logger.info(f"正在生成概念配置: {concept_name}")
            concept_profile: Dict[str, Any] = self._generate_concept_profile(concept_name, concept_papers)  # type: ignore
            
            if not concept_profile:
                self.logger.error("概念配置生成失败")
                return False
            
            # 保存概念配置
            concept_profile_file: str = os.path.join(self.output_dir, f"{self.project_name}_concept_profile.json")  # type: ignore
            with open(concept_profile_file, 'w', encoding='utf-8') as f:  # type: ignore
                json.dump(concept_profile, f, ensure_ascii=False, indent=2)
            
            self.logger.success(f"概念配置已保存: {concept_profile_file}")
            return True
            
        except Exception as e:
            self.logger.error(f"概念学习阶段失败: {e}")
            return False
    
    
    def _fix_json_string(self, json_str: str) -> str:
        """修复常见的JSON字符串问题"""
        try:
            # 移除可能的注释
            import re
            json_str = re.sub(r'//.*', '', json_str)  # 移除单行注释
            json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)  # 移除多行注释
            
            # 修复常见的JSON格式问题
            json_str = json_str.strip()
            
            # 如果字符串以引号开始但不以引号结束，添加结束引号
            if json_str.startswith('"') and not json_str.endswith('"'):
                json_str += '"'
            elif json_str.startswith("'") and not json_str.endswith("'"):
                json_str += "'"
            
            return json_str
        except Exception as e:
            self.logger.error(f"修复JSON字符串失败: {e}")
            return json_str
    
    def _generate_concept_profile(self, concept_name: str, concept_papers: list[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore
        """根据已分析的种子论文摘要，生成概念配置文件。"""
        try:
            self.logger.info(f"开始生成概念学习笔记: {concept_name}")
            self.logger.info(f"种子论文数量: {len(concept_papers)}")
            
            # 1. 加载概念分析的 Prompt 模板
            try:
                with open('prompts/prompt_prime_concept.txt', 'r', encoding='utf-8') as f:
                    prompt_template = f.read()
                self.logger.success(f"加载概念分析提示词模板: {len(prompt_template)}字符")
            except Exception as e:
                self.logger.error(f"无法加载概念分析提示词模板: {e}")
                return {}  # type: ignore

            # 2. 准备论文数据 (直接从传入的 concept_papers 构建)
            papers_data: list[Dict[str, Any]] = []  # type: ignore
            for paper in concept_papers:  # type: ignore
                # 从 paper['ai_summary'] 提取所需字段，构建 papers_data
                papers_data.append({
                    'file_name': paper.get('file_name', '未知文件'),  # type: ignore
                    'ai_summary': paper.get('ai_summary', {})  # type: ignore
                })
            
            # 3. 构建最终的 Prompt
            papers_json = json.dumps(papers_data, ensure_ascii=False, indent=2)
            final_prompt = prompt_template.replace('{{CONCEPT_NAME}}', concept_name).replace('{{SEED_PAPERS}}', papers_json)
            
            # 调用AI生成概念学习笔记
            writer_config: Dict[str, Any] = (self.config or {}).get('Writer_API', {})  # type: ignore
            writer_api_config: APIConfig = {
                'api_key': writer_config.get('api_key') or '',  # type: ignore
                'model': writer_config.get('model') or '',  # type: ignore
                'api_base': writer_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
            }
            
            # 设置系统提示词
            system_prompt = """你是一位学术研究专家，专门研究概念的历史发展和理论演化。请基于提供的种子论文，生成一个关于指定概念的全面学习笔记，并以JSON格式返回。"""
            
            self.logger.info("正在调用AI生成概念学习笔记...")
            
            # 使用ai_interface.py中的健壮API调用函数
            from ai_interface import _call_ai_api  # type: ignore
            concept_profile = _call_ai_api(
                prompt=final_prompt,
                api_config=writer_api_config,
                system_prompt=system_prompt,
                max_tokens=4000,
                temperature=0.7,
                response_format="json"
            )
            
            if concept_profile:
                self.logger.success(f"概念学习笔记生成成功")
                return concept_profile  # type: ignore
            else:
                self.logger.error("概念学习笔记生成失败")
                return {}  # type: ignore
            
        except Exception as e:
            self.logger.error(f"生成概念配置失败: {e}")
            return {}  # type: ignore
    
    
    
    
    def run_concept_priming(self, seed_papers_folder: str, concept_name: str) -> bool:
        """运行概念学习阶段（保留旧函数名以兼容）"""
        return self.run_priming_phase(concept_name, seed_papers_folder)
        
        
        


def sanitize_path_component(path_component: str) -> str:
    """清理路径组件，移除或替换非法字符"""
    import re
    if not path_component:
        return "unnamed"
    
    # 移除或替换Windows路径中的非法字符
    # Windows不允许的字符: < > : " | ? * 以及控制字符
    sanitized = re.sub(r'[<>:"|?*\x00-\x1f]', '_', path_component)
    
    # 移除开头和结尾的空格和点（Windows不允许）
    sanitized = sanitized.strip(' .')
    
    # 确保名称不为空
    if not sanitized:
        sanitized = "unnamed"
    
    # 限制长度（Windows路径限制）
    if len(sanitized) > 100:
        sanitized = sanitized[:100]
    
    return sanitized

def dispatch_command(args: argparse.Namespace):  # type: ignore
    """命令分派器 - 根据参数调用相应的处理函数"""
    try:
        # 检查是否为安装模式
        if args.setup:
            run_setup_wizard()
            return
        
        # 概念学习模式（Priming Phase）
        if args.prime_with_folder and args.concept:
            # 检查是否提供了项目名称
            if not args.project_name:
                logging.error("概念学习模式需要指定 --project-name 参数")
                sys.exit(1)
            
            generator = LiteratureReviewGenerator(args.config, args.project_name, None)
            generator.logger.info("*** 概念学习模式已启动 ***")
            generator.logger.info("=" * 60)
            
            if not generator.load_configuration():
                generator.logger.error("配置加载失败")
                sys.exit(1)
            
            # 设置输出目录
            if not generator.setup_output_directory():
                generator.logger.error("输出目录设置失败")
                sys.exit(1)
            
            # 执行概念学习阶段
            success = generator.run_priming_phase(args.concept, args.prime_with_folder)
            if success:
                generator.logger.success("概念学习阶段完成！概念配置文件已生成")
            else:
                generator.logger.error("概念学习阶段失败")
                sys.exit(1)
            return
        
        # 重试模式
        if args.retry_failed:
            handle_retry_failed(args)
            return
        
        # 合并模式
        if args.merge:
            handle_merge_mode(args)
            return
        
        # 正常执行模式 - 验证参数
        if not args.project_name and not args.pdf_folder:
            logging.error("必须指定--project-name或--pdf-folder参数中的一个")
            sys.exit(1)
            
        generator = LiteratureReviewGenerator(args.config, args.project_name, args.pdf_folder)
        
        # 先加载配置和设置输出目录
        if not generator.load_configuration():
            generator.logger.error("配置加载失败")
            sys.exit(1)
        
        if not generator.setup_output_directory():
            generator.logger.error("输出目录设置失败")
            sys.exit(1)
        
        # 概念模式验证
        if args.concept and not args.prime_with_folder:
            generator.logger.info(f"检测到概念模式，概念名称: {args.concept}")
            # 设置概念模式标志
            generator.concept_mode = True
            
            # 尝试加载概念配置文件
            concept_profile_file: str = os.path.join(generator.output_dir or '', f'{generator.project_name or "concept"}_concept_profile.json')  # type: ignore
            if os.path.exists(concept_profile_file):  # type: ignore
                try:
                    with open(concept_profile_file, 'r', encoding='utf-8') as f:  # type: ignore
                        generator.concept_profile = json.load(f)
                    generator.logger.success(f"概念配置文件已加载: {concept_profile_file}")
                except Exception as e:
                    generator.logger.error(f"加载概念配置文件失败: {e}")
                    generator.concept_profile = None
            else:
                generator.logger.warning(f"未找到概念配置文件: {concept_profile_file}")
                generator.logger.warning("概念增强分析将无法执行，请先运行概念学习阶段")
                generator.concept_profile = None
        
        # 一键执行模式
        if args.run_all:
            handle_run_all_mode(generator)
        # 原有的单独执行模式
        elif args.generate_outline:
            handle_generate_outline_mode(generator, args)
        elif args.generate_review:
            handle_generate_review_mode(generator)
        elif args.validate_review:
            if generator.load_existing_summaries():
                 validator.run_review_validation(generator)  # type: ignore
            else:
                generator.logger.error("无法加载摘要文件，请先运行阶段一")
                sys.exit(1)
        else:
            # 默认执行阶段一
            handle_stage_one_mode(generator, args)
            
    except KeyboardInterrupt:
        logging.info("用户中断程序")
        sys.exit(1)
    except Exception as e:
        logging.error(f"程序运行失败: {e}")
        logging.error("=" * 60)
        logging.error("详细错误信息:")
        logging.error(traceback.format_exc())
        logging.error("=" * 60)

        # 检查是否为网络相关异常
        import requests  # type: ignore
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException)):
            logging.error("检测到网络连接中断。")
            logging.error("不用担心，您的进度已被保存。")
            logging.error("请在网络恢复后，重新运行您刚才使用的命令，程序将从中断的地方继续。")
        else:
            logging.error("请检查配置文件、网络连接和文件路径是否正确")

        sys.exit(1)

def parse_failure_report(failure_report_file: str, pdf_folder: Optional[str] = None) -> List[PaperInfo]:  # type: ignore
    """从失败报告文件中解析失败的论文信息"""
    try:
        with open(failure_report_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        papers: List[PaperInfo] = []
        
        # 查找论文标题
        import re
        title_pattern = r'📄 标题:\s*(.+?)(?:\r?\n|$)'
        title_matches: List[str] = re.findall(title_pattern, content)
        
        for title in title_matches:
            title = title.strip()
            if title:
                logging.info(f"从失败报告中提取到论文标题: {title}")
                
                # PDF文件夹路径已经作为参数传入
                logging.info(f"PDF文件夹路径: {pdf_folder}")
                
                # 如果找到了PDF文件夹，在其中搜索
                pdf_path = None
                if pdf_folder and os.path.exists(pdf_folder):
                    import glob
                    # 在文件夹中搜索包含标题的PDF文件
                    pattern = os.path.join(pdf_folder, '**', '*.pdf')
                    all_pdfs = glob.glob(pattern, recursive=True)
                    
                    logging.info(f"在PDF文件夹中找到 {len(all_pdfs)} 个PDF文件")
                    
                    for pdf_file in all_pdfs:
                        pdf_filename = os.path.splitext(os.path.basename(pdf_file))[0]
                        
                        # 简化的匹配逻辑：检查作者姓名和关键内容
                        # 直接检查作者姓名（通常是最可靠的匹配方式）
                        author_match = False
                        title_keywords: List[str] = []
                        
                        # 提取论文标题中的作者姓名（通常在最后，用下划线分隔）
                        if '_' in title:
                            possible_authors = title.split('_')[-1].strip()
                            if possible_authors and possible_authors in pdf_filename:
                                author_match = True
                                logging.info(f"基于作者姓名匹配: {possible_authors}")
                        
                        # 提取关键内容词汇
                        if "中国农田建设政策" in title:
                            title_keywords.extend(["中国农田建设政策", "农田建设政策"])
                        if "一号文件" in title:
                            title_keywords.append("一号文件")
                        if "三农" in title:
                            title_keywords.append("三农")
                        if "朱华东" in title:
                            title_keywords.append("朱华东")
                        
                        # 检查PDF文件名是否包含关键词
                        keyword_matches = 0
                        matched_words: List[str] = []
                        for keyword in title_keywords:
                            if keyword in pdf_filename:
                                keyword_matches += 1
                                matched_words.append(keyword)
                        
                        # 如果作者姓名匹配或关键词匹配足够多，则认为匹配成功
                        if author_match or keyword_matches >= 2:
                            pdf_path = pdf_file
                            logging.info(f"成功匹配PDF文件: {pdf_file}")
                            if author_match:
                                logging.info("匹配原因: 作者姓名")
                            if keyword_matches > 0:
                                logging.info(f"匹配到 {keyword_matches} 个关键词: {matched_words}")
                            break
                        
                        # 最后的备选方案：直接比较（去除特殊字符）
                        else:
                            clean_title = title.replace('——', '').replace('_', '').replace('"', '').replace('（', '').replace('）', '')
                            clean_filename = pdf_filename.replace('_', '').replace('"', '').replace('（', '').replace('）', '')
                            
                            # 如果标题中的大部分内容在文件名中
                            if len(clean_title) > 10 and clean_title in clean_filename:
                                pdf_path = pdf_file
                                logging.info(f"基于整体字符串匹配找到PDF文件: {pdf_file}")
                                break
                
                # 如果找到了PDF文件，创建论文信息
                if pdf_path and os.path.exists(pdf_path):
                    paper_info: PaperInfo = {
                        'title': title,
                        'authors': [],
                        'year': '未知年份',
                        'journal': '未知期刊',
                        'doi': '',
                        'pdf_path': pdf_path,
                        'file_index': 0
                    }
                    papers.append(paper_info)
                    logging.info(f"成功创建失败论文的重试信息: {title}")
                else:
                    logging.warning(f"未找到论文标题对应的PDF文件: {title}")
                    logging.info(f"PDF文件夹: {pdf_folder}")
                    logging.info(f"PDF文件夹是否存在: {os.path.exists(pdf_folder) if pdf_folder else 'None'}")
        
        # 如果还是没有找到PDF路径，查找PDF文件路径的模式
        if not papers:
            pdf_pattern = r'PDF文件不存在:\s*(.+\.pdf)'
            pdf_matches: List[str] = re.findall(pdf_pattern, content)
            
            for pdf_path in pdf_matches:
                pdf_path = pdf_path.strip()
                if pdf_path and os.path.exists(pdf_path):
                    title = os.path.splitext(os.path.basename(pdf_path))[0]
                    
                    paper_info: PaperInfo = {
                        'title': title,
                        'authors': [],
                        'year': '未知年份',
                        'journal': '未知期刊',
                        'doi': '',
                        'pdf_path': pdf_path,
                        'file_index': 0
                    }
                    papers.append(paper_info)
        
        return papers
        
    except Exception as e:
        logging.error(f"解析失败报告文件出错: {e}")
        return []

def handle_retry_failed(args: argparse.Namespace):  # type: ignore
    """处理重试失败论文模式"""
    if not args.project_name and not args.pdf_folder:
        logging.error("使用--retry-failed命令时必须提供--project-name或--pdf-folder参数中的一个")
        sys.exit(1)

    generator = LiteratureReviewGenerator(args.config, args.project_name, args.pdf_folder)
    generator.logger.info("*** 失败论文重试模式已启动 ***")
    
    if not generator.load_configuration() or not generator.setup_output_directory():
        sys.exit(1)

    if not generator.load_existing_summaries():
        generator.logger.error("未找到摘要文件，无法进行重试。请先运行一次完整的分析。")
        sys.exit(1)

    papers_to_retry = []
    retry_report_file = ''  # 初始化变量
    if generator.mode == "zotero":
        retry_report_file: str = os.path.join(generator.output_dir or '', f'{generator.project_name or "project"}_zotero_report_for_retry.txt')  # type: ignore
        if not os.path.exists(retry_report_file):  # type: ignore:
            generator.logger.error(f"Zotero模式重试失败：未找到重跑报告文件 '{retry_report_file}'")
            sys.exit(1)
        papers_to_retry = parse_zotero_report(retry_report_file)  # type: ignore
    else:  # direct mode
        generator.logger.info("直接PDF模式：正在从摘要文件和失败报告中识别失败的论文...")
        
        # 首先尝试从summaries.json中查找失败的论文
        failed_summaries = [s for s in generator.summaries if s.get('status') == 'failed']  # type: ignore
        papers_to_retry = [s.get('paper_info') for s in failed_summaries if s.get('paper_info')]  # type: ignore
        
        # 如果没有在summaries.json中找到失败的论文，尝试从失败报告文件中读取
        if not papers_to_retry:
            generator.logger.info("在summaries.json中未找到失败的论文，正在检查失败报告...")
            failure_report_file = os.path.join(generator.output_dir or '', f'{generator.project_name or "project"}_failed_papers_report.txt')
            
            if os.path.exists(failure_report_file):
                generator.logger.info(f"找到失败报告文件: {failure_report_file}")
                try:
                    # 解析失败报告文件，传入PDF文件夹路径
                    failed_papers_from_report = parse_failure_report(failure_report_file, generator.pdf_folder)
                    if failed_papers_from_report:
                        papers_to_retry = failed_papers_from_report
                        generator.logger.info(f"从失败报告中提取到 {len(papers_to_retry)} 篇需要重试的论文")
                    else:
                        generator.logger.warning("失败报告文件存在但无法解析")
                except Exception as e:
                    generator.logger.error(f"读取失败报告文件失败: {e}")
            else:
                generator.logger.warning(f"未找到失败报告文件: {failure_report_file}")

    if not papers_to_retry:
        generator.logger.success("没有找到需要重试的失败论文。")
        return

    generator.logger.info(f"识别到 {len(papers_to_retry)} 篇论文需要重试。")
    
    original_summary_count = len(generator.summaries)
    file_index_path: str = generator.config.get('Paths', {}).get('library_path', '') if generator.mode == 'zotero' and generator.config else generator.pdf_folder or ''  # type: ignore
    file_index = create_file_index(file_index_path)  # type: ignore
    performance_config = generator.config.get('Performance') or {}  # type: ignore
    max_workers = int(performance_config.get('max_workers', 3))  # type: ignore

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:  # type: ignore
        future_to_paper = {executor.submit(generator.process_paper, paper, i, file_index, len(papers_to_retry)): paper for i, paper in enumerate(papers_to_retry)}  # type: ignore
        progress_bar = tqdm(concurrent.futures.as_completed(future_to_paper), total=len(papers_to_retry), desc="[重试模式] 正在处理")  # type: ignore
        for future in progress_bar:
            result: Optional[Dict[str, Any]] = future.result()  # type: ignore
            if result and result.get('status') == 'success':  # type: ignore
                # 在直接PDF模式下，更新原始条目而不是添加新条目
                if generator.mode == "direct":
                    paper_key = LiteratureReviewGenerator.get_paper_key(result.get('paper_info', {}))  # type: ignore
                    # 查找并更新原始条目
                    for i, summary in enumerate(generator.summaries):
                        if LiteratureReviewGenerator.get_paper_key(summary.get('paper_info', {})) == paper_key:  # type: ignore
                            generator.summaries[i] = result  # type: ignore
                            break
                    else:
                        # 如果没有找到原始条目，则添加新条目
                        generator.summaries.append(result)  # type: ignore
                else:
                    # Zotero模式下，直接添加新条目
                    generator.summaries.append(result)  # type: ignore
            else:
                # 处理失败的论文
                failed_paper: Dict[str, Any] = result or {'paper_info': future_to_paper[future], 'failure_reason': '未知重试错误'}  # type: ignore
                if generator.mode == "direct":
                    paper_key = LiteratureReviewGenerator.get_paper_key(failed_paper.get('paper_info', {}))
                    # 查找并更新原始条目
                    for i, summary in enumerate(generator.summaries):
                        if LiteratureReviewGenerator.get_paper_key(summary.get('paper_info', {})) == paper_key:
                            generator.summaries[i] = failed_paper  # type: ignore
                            break
                    else:
                        # 如果没有找到原始条目，则添加新条目
                        generator.summaries.append(failed_paper)  # type: ignore
                else:
                    # Zotero模式下，直接添加到失败列表
                    generator.failed_papers.append(failed_paper)  # type: ignore

    generator.save_summaries()
    
    # 调用统一的报告生成方法
    generator.generate_all_reports()

    # 计算新增成功的论文数量
    success_count = len([s for s in generator.summaries if s.get('status') == 'success'])  # type: ignore
    original_success = len([s for s in generator.summaries[:original_summary_count] if s.get('status') == 'success'])  # type: ignore
    newly_succeeded = success_count - original_success
    failed_count = len([s for s in generator.summaries if s.get('status') == 'failed'])  # type: ignore
    generator.logger.success(f"重试完成！新增成功 {newly_succeeded} 篇，仍然失败 {failed_count} 篇。")  # type: ignore
    
    if not generator.failed_papers and generator.mode == 'zotero' and os.path.exists(retry_report_file):
        try:
            os.remove(retry_report_file)
            generator.logger.info(f"所有失败论文均已成功重试，已自动删除重跑报告文件: {retry_report_file}")
        except Exception as e:
            generator.logger.warning(f"无法自动删除重跑报告文件: {e}")

def handle_merge_mode(args: argparse.Namespace):  # type: ignore
    """处理合并模式"""
    # 验证参数：必须提供project_name或pdf_folder中的一个
    if not args.project_name and not args.pdf_folder:
        logging.error("使用--merge命令时必须提供--project-name或--pdf-folder参数中的一个")
        sys.exit(1)
    
    generator = LiteratureReviewGenerator(args.config, args.project_name, args.pdf_folder)
    generator.logger.info("*** 合并模式已启动 ***")
    generator.logger.info("=" * 60)
    
    # 根据模式确定项目名称和文件路径
    try:
        # 加载配置以获取输出路径
        if not generator.load_configuration():
            generator.logger.error("配置加载失败")
            sys.exit(1)
        
        # 设置输出目录以确定项目名称
        if not generator.setup_output_directory():
            generator.logger.error("输出目录设置失败")
            sys.exit(1)
        
        # 确定主文件路径
        main_file = generator.summary_file
        merge_file = args.merge
        
        if not main_file or not os.path.exists(main_file):
            generator.logger.error(f"主文件不存在: {main_file}")
            return
        
        if not os.path.exists(merge_file):
            generator.logger.error(f"合并文件不存在: {merge_file}")
            return
        
        # 读取两个文件
        with open(main_file, 'r', encoding='utf-8') as f:  # type: ignore
            main_data = json.load(f)  # type: ignore
        
        with open(merge_file, 'r', encoding='utf-8') as f:  # type: ignore
            merge_data = json.load(f)  # type: ignore
        
        if not isinstance(main_data, list) or not isinstance(merge_data, list):  # type: ignore
            generator.logger.error("文件格式错误，必须是JSON数组")
            return
        
        # 智能合并：以合并文件中的记录为准
        generator.logger.info(f"主文件包含 {len(main_data)} 篇论文")  # type: ignore
        generator.logger.info(f"合并文件包含 {len(merge_data)} 篇论文")  # type: ignore
        
        # 创建基于DOI的索引（如果没有DOI则使用标题+作者）
        def get_paper_key(paper: 'Dict[str, Any] | PaperInfo'):  # type: ignore
            paper_info = paper.get('paper_info', {})  # type: ignore
            return paper_info.get('doi', f"{paper_info.get('title', '')}_{paper_info.get('authors', [])}")  # type: ignore
        
        # 构建主文件的索引
        main_index = {get_paper_key(paper): i for i, paper in enumerate(main_data)}  # type: ignore
        
        # 合并数据
        merged_count = 0
        added_count = 0
        
        for merge_paper in merge_data:  # type: ignore
            merge_key = get_paper_key(merge_paper)  # type: ignore
            
            if merge_key in main_index:
                # 更新现有记录
                main_index_pos = main_index[merge_key]
                main_data[main_index_pos] = merge_paper  # type: ignore
                merged_count += 1
            else:
                # 添加新记录
                main_data.append(merge_paper)  # type: ignore
                added_count += 1
        
        # 保存合并结果
        backup_file: str = f"{main_file}.backup.{int(time.time())}"  # type: ignore
        os.rename(main_file, backup_file)  # type: ignore
        generator.logger.info(f"已创建备份文件: {backup_file}")  # type: ignore
        
        with open(main_file, 'w', encoding='utf-8') as f:  # type: ignore
            json.dump(main_data, f, ensure_ascii=False, indent=2)  # type: ignore
        
        generator.logger.success("合并完成！")  # type: ignore
        generator.logger.info(f"更新记录: {merged_count} 篇")  # type: ignore
        generator.logger.info(f"新增记录: {added_count} 篇")  # type: ignore
        generator.logger.info(f"总记录数: {len(main_data)} 篇")  # type: ignore
        
    except Exception as e:
        generator.logger.error(f"合并过程中出错: {e}")
        traceback.print_exc()

def handle_run_all_mode(generator: 'LiteratureReviewGenerator'):  # type: ignore
    """处理一键执行模式"""
    generator.logger.info("*** '一键执行'模式已启动 ***")
    generator.logger.info("=" * 60)
    
    # 执行阶段一
    generator.logger.info("开始执行阶段一：文献分析...")
    stage1_success = generator.run_stage_one()
    
    if stage1_success:
        generator.logger.success("\n阶段一执行成功！")
        generator.logger.info("开始执行阶段二：文献综述生成...")
        
        # 执行阶段二：先生成大纲，再生成全文
        generator.logger.info("开始执行阶段二第一步：生成大纲...")
        outline_success = generator.generate_literature_review_outline()
        
        if outline_success:
            generator.logger.success("大纲生成成功！")
            generator.logger.info("开始执行阶段二第二步：从大纲生成全文...")
            stage2_success = generator.generate_full_review_from_outline()
        else:
            stage2_success = False
        
        if stage2_success:
            generator.logger.success("\n一键执行模式完成！所有任务执行成功！")
        else:
            generator.logger.error("\n阶段二执行失败！")
            sys.exit(1)
    else:
        generator.logger.error("\n阶段一执行失败，无法继续执行阶段二！")
        sys.exit(1)

def handle_generate_outline_mode(generator: 'LiteratureReviewGenerator', args: argparse.Namespace):  # type: ignore
    """处理生成大纲模式"""
    success = generator.generate_literature_review_outline()
    if success:
        generator.logger.success("\n大纲生成成功！文献综述大纲已生成完成")
        generator.logger.info(f"您可以编辑大纲文件，然后运行以下命令生成完整综述：")
        if args.project_name:
            # 检查是否是概念模式
            if args.concept:
                generator.logger.info(f"命令: python main.py --project-name \"{args.project_name}\" --concept \"{args.concept}\" --generate-review")
            else:
                generator.logger.info(f"命令: python main.py --project-name \"{args.project_name}\" --generate-review")
        elif args.pdf_folder:
            # 检查是否是概念模式
            if args.concept:
                generator.logger.info(f"命令: python main.py --pdf-folder \"{args.pdf_folder}\" --concept \"{args.concept}\" --generate-review")
            else:
                generator.logger.info(f"命令: python main.py --pdf-folder \"{args.pdf_folder}\" --generate-review")
    else:
        generator.logger.error("\n大纲生成失败！")
        sys.exit(1)

def handle_generate_review_mode(generator: 'LiteratureReviewGenerator'):  # type: ignore
    """处理生成综述模式"""
    success = generator.generate_full_review_from_outline()
    if success:
        generator.logger.success("\n文献综述生成成功！完整综述已生成完成")
    else:
        generator.logger.error("\n文献综述生成失败！")
        sys.exit(1)

def handle_stage_one_mode(generator: 'LiteratureReviewGenerator', args: argparse.Namespace):  # type: ignore
    """处理阶段一模式（默认模式）"""
    generator.logger.info("*** 阶段一模式已启动 ***")
    generator.logger.info("=" * 60)
    
    # 执行阶段一
    generator.logger.info("开始执行阶段一：文献分析...")
    stage1_success = generator.run_stage_one()
    
    if stage1_success:
        generator.logger.success("\n阶段一执行成功！")
        generator.logger.info("您现在可以继续执行以下命令：")
        if args.project_name:
            # 检查是否是概念模式
            if args.concept:
                generator.logger.info(f"生成大纲: python main.py --project-name \"{args.project_name}\" --concept \"{args.concept}\" --generate-outline")
                generator.logger.info(f"一键生成综述: python main.py --project-name \"{args.project_name}\" --concept \"{args.concept}\" --run-all")
            else:
                generator.logger.info(f"生成大纲: python main.py --project-name \"{args.project_name}\" --generate-outline")
                generator.logger.info(f"一键生成综述: python main.py --project-name \"{args.project_name}\" --run-all")
        elif args.pdf_folder:
            # 检查是否是概念模式
            if args.concept:
                generator.logger.info(f"生成大纲: python main.py --pdf-folder \"{args.pdf_folder}\" --concept \"{args.concept}\" --generate-outline")
                generator.logger.info(f"一键生成综述: python main.py --pdf-folder \"{args.pdf_folder}\" --concept \"{args.concept}\" --run-all")
            else:
                generator.logger.info(f"生成大纲: python main.py --pdf-folder \"{args.pdf_folder}\" --generate-outline")
                generator.logger.info(f"一键生成综述: python main.py --pdf-folder \"{args.pdf_folder}\" --run-all")
    else:
        generator.logger.error("\n阶段一执行失败！")
        sys.exit(1)


def main() -> None:  # type: ignore
    """主函数，处理命令行参数和执行相应操作"""
    
    parser = argparse.ArgumentParser(
        description="llm_reviewer_generator - 文献综述自动生成器",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    parser.add_argument(
        '--config',
        type=str,
        default='config.ini',
        help='Path to the configuration file.'
    )
    parser.add_argument(
        '--project-name', 
        type=str, 
        help='为您的项目指定一个唯一的名称，用于创建独立的输出文件夹。'
    )
    parser.add_argument(
        '--pdf-folder', 
        type=str, 
        help='直接指定包含PDF文件的文件夹路径，llm_reviewer_generator将扫描并处理这些文件。'
    )
    parser.add_argument(
        '--run-all', 
        action='store_true', 
        help='一键运行所有阶段：从文献分析到最终生成Word版文献综述。'
    )
    parser.add_argument(
        '--analyze-only', 
        action='store_true', 
        help='仅运行阶段一：分析文献并生成摘要。'
    )
    parser.add_argument(
        '--generate-outline', 
        action='store_true', 
        help='仅运行阶段二：根据现有摘要生成文献综述大纲。'
    )
    parser.add_argument(
        '--generate-review', 
        action='store_true', 
        help='仅运行阶段三：根据现有大纲和摘要生成完整的Word版文献综述。'
    )
    parser.add_argument(
        '--validate-review',
        action='store_true',
        help='（在综述生成后运行）对生成的Word综述进行引用和观点验证。'
    )
    parser.add_argument(
        '--setup', 
        action='store_true', 
        help='运行交互式设置向导，创建或更新config.ini文件。'
    )
    parser.add_argument('--prime-with-folder', type=str, help='Path to a folder with seed papers for concept priming.')
    parser.add_argument('--concept', type=str, help='The name of the concept to be primed.')
    parser.add_argument('--retry-failed', action='store_true', help='Retry processing failed papers from a previous run.')
    parser.add_argument('--merge', type=str, help='Path to a summaries.json file to merge into the main project.')

    args = parser.parse_args()
    dispatch_command(args)

if __name__ == "__main__":
    main()
