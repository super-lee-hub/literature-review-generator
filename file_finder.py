import os
import re
import threading
import logging
from typing import Dict, Any, Optional, List, Tuple
import unicodedata

# 设置日志记录器
logger = logging.getLogger(__name__)


class FileIndex:
    """
    单例模式的文件索引类，用于精确查找PDF文件
    在程序启动时一次性扫描Zotero存储目录，建立内存索引
    支持中文文件名和特殊字符的高效查找
    增强线程安全性，支持并发访问
    """
    _instance = None
    _creation_lock = threading.Lock()  # 用于实例创建的锁
    _initialization_lock = threading.Lock()  # 用于初始化的锁
    _init_lock: threading.Lock
    _initialized: bool
    library_path: Optional[str]
    file_index: Dict[str, str]
    original_names: Dict[str, str]
    _access_lock: threading.Lock

    def __new__(cls, library_path: Optional[str] = None):
        # 使用双重检查锁定模式确保线程安全的单例实现
        if cls._instance is None:
            with cls._creation_lock:
                if cls._instance is None:
                    cls._instance = super(FileIndex, cls).__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._init_lock = threading.Lock()  # 实例级别的初始化锁
        return cls._instance

    def __init__(self, library_path: Optional[str] = None):
        # 防止重复初始化
        if self._initialized:
            return

        # 使用实例级别的锁确保初始化只执行一次
        with self._init_lock:
            if self._initialized:
                return

            self.library_path = library_path
            self.file_index: Dict[str, str] = {}  # {标准化文件名: 完整路径}
            self.original_names: Dict[str, str] = {}  # {标准化文件名: 原始文件名}
            self._access_lock = threading.Lock()  # 用于保护数据访问的锁

            if library_path:
                try:
                    self._build_index()
                    self._initialized = True
                except Exception as e:
                    logger.error(f"文件索引初始化失败: {e}")
                    # 即使初始化失败，也标记为已初始化，避免重复尝试
                    self._initialized = True

    def __len__(self):
        """返回索引中文件的数量"""
        return len(self.file_index)

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        """
        标准化文件名，用于不区分大小写的比较
        处理中文、特殊字符和Unicode标准化

        Args:
            filename: 原始文件名

        Returns:
            标准化后的文件名
        """
        # Unicode标准化（NFC形式）
        normalized = unicodedata.normalize('NFC', filename)
        # 转换为小写
        return normalized.lower()

    def _build_index(self) -> None:
        """构建文件索引"""
        if self.library_path is None:
            logger.error("library_path为None，无法构建索引")
            return
            
        logger.info(f"正在构建文件索引，扫描路径: {self.library_path}")

        try:
            # 检查是否是标准的Zotero storage目录（包含子文件夹）
            # 或者是测试目录（直接包含PDF文件）
            storage_subdirs = [d for d in os.listdir(self.library_path)
                              if os.path.isdir(os.path.join(self.library_path, d))]
            
            total_files = 0
            
            if storage_subdirs:
                # 标准Zotero storage目录结构：包含多个子文件夹
                logger.info(f"发现 {len(storage_subdirs)} 个存储子文件夹")
                
                for subdir in storage_subdirs:
                    subdir_path: str = os.path.join(self.library_path, subdir)

                    try:
                        # 获取该子文件夹中的所有文件
                        files: List[str] = os.listdir(subdir_path)

                        # 为每个PDF文件建立索引
                        for filename in files:
                            if filename.lower().endswith('.pdf'):
                                file_path: str = os.path.join(subdir_path, filename)
                                # 使用标准化文件名作为键
                                normalized_name: str = FileIndex._normalize_filename(filename)
                                self.file_index[normalized_name] = file_path
                                self.original_names[normalized_name] = filename
                                total_files += 1

                    except Exception as e:
                        # 忽略无法访问的子文件夹，继续处理其他文件夹
                        logger.warning(f"无法访问子文件夹 {subdir}: {e}")
                        continue
            else:
                # 测试目录结构：直接包含PDF文件
                logger.info(f"检测到直接PDF文件目录结构")
                
                try:
                    # 获取目录中的所有文件
                    files: List[str] = os.listdir(self.library_path)

                    # 为每个PDF文件建立索引
                    for filename in files:
                        if filename.lower().endswith('.pdf'):
                            file_path: str = os.path.join(self.library_path, filename)
                            # 使用标准化文件名作为键
                            normalized_name: str = FileIndex._normalize_filename(filename)
                            self.file_index[normalized_name] = file_path
                            self.original_names[normalized_name] = filename
                            total_files += 1

                except Exception as e:
                    logger.error(f"无法访问目录 {self.library_path}: {e}")

            logger.info(f"文件索引构建完成，共索引 {total_files} 个PDF文件")

        except Exception as e:
            logger.error(f"构建文件索引失败: {e}")

    def find_exact(self, filename: str) -> Optional[str]:
        """
        在索引中精确查找文件（按名索骥）

        Args:
            filename: 要查找的文件名（不区分大小写）

        Returns:
            文件的完整路径，如果找不到则返回None
        """
        if not filename:
            return None

        # 提取纯文件名（去掉路径）
        basename: str = os.path.basename(filename)

        # 标准化文件名
        normalized_name: str = FileIndex._normalize_filename(basename)

        # 精确查找文件
        return self.file_index.get(normalized_name)

    def find_fuzzy(self, keywords: List[str]) -> List[Tuple[str, str]]:
        """
        模糊查找文件（基于关键词）

        Args:
            keywords: 关键词列表

        Returns:
            [(文件名, 完整路径), ...] 列表
        """
        results: List[Tuple[str, str]] = []
        for norm_name, file_path in self.file_index.items():
            match_count: int = sum(1 for kw in keywords if kw.lower() in norm_name)
            if match_count >= min(3, len(keywords)):  # 至少匹配3个关键词或全部关键词
                results.append((self.original_names[norm_name], file_path))
        return results


def _is_translation(filename: str) -> bool:
    """判断文件是否为翻译版本"""
    translation_keywords: List[str] = ['中文翻译', '翻译版', 'chinese translation', '译版']
    filename_lower: str = filename.lower()
    return any(keyword.lower() in filename_lower for keyword in translation_keywords)


def _is_supplement(filename: str) -> bool:
    """判断文件是否为补充材料（严格检测）"""
    # 只检测明确的补充材料关键词
    supplement_keywords: List[str] = ['supplementary material', 'appendix', 'SI.pdf', 'supporting information',
                          'supplement.pdf']
    filename_lower: str = filename.lower()
    return any(keyword.lower() in filename_lower for keyword in supplement_keywords)


def _score_pdf_quality(file_path: str, filename: str, title: str = "") -> Tuple[float, str]:
    """
    对PDF文件进行质量评分（宽松版本 - 只要能找到就尽量用）

    Args:
        file_path: PDF文件完整路径
        filename: 文件名
        title: 论文标题（用于文件名匹配度检测）

    Returns:
        (score, diagnostic_info): 分数（0-100）和诊断信息字符串
    """
    score: float = 100.0
    diagnostics: List[str] = []

    # 1. 文件大小检测（只拒绝极小文件）
    try:
        file_size: float = os.path.getsize(file_path) / 1024  # KB
        if file_size < 1:  # 只拒绝小于1KB的文件（测试用）
            score = 0.0
            diagnostics.append(f"文件过小({int(file_size)}KB)")
        elif file_size < 10:
            score -= 5.0  # 轻微扣分
            diagnostics.append(f"文件较小({int(file_size)}KB)")
    except OSError:
        pass

    # 2. 翻译版本检测（轻微扣分）
    if _is_translation(filename):
        score -= 10.0  # 从50降低到10
        diagnostics.append("或为翻译版本")

    # 3. 补充材料检测（中等扣分）
    if _is_supplement(filename):
        score -= 30.0  # 从80降低到30
        diagnostics.append("或为补充材料")

    # 4. 文件名与标题匹配度（加分项）
    if title:
        # 简单的相似度检测：计算标题中关键词在文件名中出现的比例
        title_words: set[str] = set(re.findall(r'\w+', title.lower()))
        filename_words: set[str] = set(re.findall(r'\w+', filename.lower()))
        if title_words:
            match_ratio: float = len(title_words & filename_words) / len(title_words)
            if match_ratio > 0.5:
                diagnostics.append("文件名匹配良好")
            elif match_ratio > 0.3:
                diagnostics.append("文件名部分匹配")

    return (max(0.0, score), "; ".join(diagnostics) if diagnostics else "质量良好")


def find_pdf(paper_meta: Dict[str, Any], library_path: str, file_index: Optional[FileIndex] = None) -> Optional[str]:
    """
    智能PDF文件查找器 - 两步决策流程（宽松版本）
    1. 广泛搜索所有候选文件（包括模糊匹配）
    2. 简单质量评估（只拒绝明显无效的文件）

    Args:
        paper_meta: 从Zotero报告中解析出的单篇文献元数据
        library_path: Zotero存储的根目录
        file_index: 可选的FileIndex实例

    Returns:
        找到的PDF文件的绝对路径，失败返回None
    """
    # 如果没有提供索引实例，创建一个
    if file_index is None:
        file_index = FileIndex(library_path)

    attachments: List[str] = paper_meta.get('attachments', [])
    title: str = paper_meta.get('title', '')
    candidates: List[Tuple[str, str]] = []
    
    # Conditional Logic Branch
    if attachments:
        # 附件列表不为空，走标准流程
        target_filenames: List[str] = []
        for attachment in attachments:
            filename: str = os.path.basename(attachment)
            if filename.startswith('o '):
                filename = filename[2:]
            target_filenames.append(filename)
            if not filename.lower().endswith('.pdf'):
                target_filenames.append(filename + '.pdf')
        
        if not target_filenames:
            logger.warning("附件列表处理后为空，转为标题匹配")
        else:
            logger.info(f"开始智能PDF查找，候选文件: {target_filenames[:5]}")
            # 步骤1: 精确匹配
            for filename in target_filenames:
                matched_path: Optional[str] = file_index.find_exact(filename)
                if matched_path:
                    candidates.append((filename, matched_path))
    
    # Fallback or Direct Fuzzy Search
    if not candidates:
        if attachments:
            logger.info("精确匹配失败，尝试基于标题的模糊匹配...")
        else:
            logger.info("[INFO] 附件列表为空，直接尝试基于标题的模糊匹配...")

        if title:
            keywords: List[str] = [w for w in re.findall(r'\w+', title) if len(w) > 3][:10]
            fuzzy_results: List[Tuple[str, str]] = file_index.find_fuzzy(keywords)
            if fuzzy_results:
                logger.info(f"模糊匹配找到 {len(fuzzy_results)} 个候选文件")
                candidates.extend(fuzzy_results)
        elif not attachments:
            # No attachments and no title
            diagnostic: str = "元数据中既无附件也无标题，无法查找文件"
            logger.error(diagnostic)
            return None

    if not candidates:
        diagnostic = "文件系统中找不到任何匹配的PDF文件。"
        logger.error(diagnostic)
        return None

    logger.info(f"找到{len(candidates)}个候选PDF文件")

    # ===== 步骤2: 简单质量评估 =====
    scored_candidates: List[Tuple[float, str, str, str]] = []
    for filename, file_path in candidates:
        score: float
        diagnostics: str
        score, diagnostics = _score_pdf_quality(file_path, filename, title)
        scored_candidates.append((score, file_path, filename, diagnostics))
        logger.info(f"候选文件: {filename}")
        logger.info(f"  - 质量分数: {score:.1f}/100")
        logger.info(f"  - 诊断: {diagnostics}")

    # ===== 步骤3: 选择最佳文件 =====
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_path, best_filename, best_diagnostics = scored_candidates[0]

    if best_score <= 0:
        diagnostic = f"所有候选文件质量分数过低(最高{best_score:.1f}/100)。最佳候选: {best_filename}, 问题: {best_diagnostics}"
        logger.error(diagnostic)
        return None

    diagnostic_info = f"选择最佳PDF: {best_filename} (分数: {best_score:.1f}/100, {best_diagnostics})"
    logger.info(diagnostic_info)
    logger.info(f"文件路径: {best_path}")
    
    return best_path


def create_file_index(library_path: str) -> FileIndex:
    """
    创建文件索引实例（推荐在main.py启动时调用一次）

    Args:
        library_path: Zotero存储的根目录

    Returns:
        FileIndex实例
        
    Raises:
        ValueError: 当路径无效时
        OSError: 当无法访问路径时
    """
    if not library_path:
        raise ValueError("library_path必须是非空字符串")
    
    # 规范化路径，防止路径遍历攻击
    library_path = os.path.normpath(library_path)
    
    if not os.path.exists(library_path):
        raise OSError(f"Zotero存储路径不存在: {library_path}")
    
    if not os.path.isdir(library_path):
        raise OSError(f"Zotero存储路径不是目录: {library_path}")
    
    try:
        # 检查目录权限
        test_file = os.path.join(library_path, '.access_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
    except Exception as e:
        raise OSError(f"无法写入Zotero存储目录，请检查权限: {e}")
    
    return FileIndex(library_path)


if __name__ == "__main__":
    # 测试函数
    import sys
    if len(sys.argv) > 2:
        library_path = sys.argv[1]
        filename = sys.argv[2]

        # 创建文件索引
        index = create_file_index(library_path)

        # 查找文件
        result = find_pdf({"attachments": [filename]}, library_path, index)

        if result and result[0]:
            logger.info(f"找到文件: {result[0]}")
            logger.info(f"诊断信息: {result[1]}")
        else:
            logger.info(f"未找到文件: {result[1] if result else '未知错误'}")
    else:
        logger.info("使用方法: python file_finder.py <library_path> <filename>")
