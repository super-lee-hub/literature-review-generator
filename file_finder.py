"""
文件查找模块。

负责在 Zotero 存储目录中查找 PDF 文件，支持：
1. 基于文件名的精确匹配
2. 基于标题关键词的模糊匹配
3. PDF 候选文件质量评分
4. 线程安全的文件索引
"""

import logging
import os
import re
import threading
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


class FileIndex:
    """
    单例模式的文件索引类，用于精确查找 PDF 文件。

    在程序启动时一次性扫描 Zotero 存储目录，建立内存索引，
    支持中文文件名和特殊字符的高效查找，并增强并发访问安全性。
    """

    _instance = None
    _creation_lock = threading.Lock()
    _initialization_lock = threading.Lock()
    _init_lock: threading.Lock
    _initialized: bool
    library_path: Optional[str]
    file_index: Dict[str, str]
    original_names: Dict[str, str]
    _access_lock: threading.Lock

    def __new__(cls, library_path: Optional[str] = None):
        """使用双重检查锁实现线程安全的单例。"""
        if cls._instance is None:
            with cls._creation_lock:
                if cls._instance is None:
                    cls._instance = super(FileIndex, cls).__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._init_lock = threading.Lock()
        return cls._instance

    def __init__(self, library_path: Optional[str] = None):
        """初始化索引；重复构造时只执行一次。"""
        if self._initialized:
            return

        with self._init_lock:
            if self._initialized:
                return

            self.library_path = library_path
            self.file_index = {}
            self.original_names = {}
            self._access_lock = threading.Lock()

            if library_path:
                try:
                    self._build_index()
                    self._initialized = True
                except Exception as exc:
                    logger.error("文件索引初始化失败: %s", exc)
                    self._initialized = True

    def __len__(self):
        """返回索引中文件数量。"""
        return len(self.file_index)

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        """标准化文件名，用于不区分大小写的比较。"""
        normalized = unicodedata.normalize("NFC", filename)
        return normalized.lower()

    def _build_index(self) -> None:
        """扫描目录并建立 PDF 文件索引。"""
        if self.library_path is None:
            logger.error("library_path 为 None，无法构建索引")
            return

        logger.info("正在构建文件索引，扫描路径: %s", self.library_path)

        try:
            storage_subdirs = [
                entry
                for entry in os.listdir(self.library_path)
                if os.path.isdir(os.path.join(self.library_path, entry))
            ]

            total_files = 0

            if storage_subdirs:
                logger.info("发现 %s 个存储子文件夹", len(storage_subdirs))

                for subdir in storage_subdirs:
                    subdir_path = os.path.join(self.library_path, subdir)

                    try:
                        files = os.listdir(subdir_path)
                        for filename in files:
                            if filename.lower().endswith(".pdf"):
                                file_path = os.path.join(subdir_path, filename)
                                normalized_name = FileIndex._normalize_filename(filename)
                                self.file_index[normalized_name] = file_path
                                self.original_names[normalized_name] = filename
                                total_files += 1
                    except Exception as exc:
                        logger.warning("无法访问子文件夹 %s: %s", subdir, exc)
                        continue
            else:
                logger.info("检测到直接 PDF 文件目录结构")

                try:
                    files = os.listdir(self.library_path)
                    for filename in files:
                        if filename.lower().endswith(".pdf"):
                            file_path = os.path.join(self.library_path, filename)
                            normalized_name = FileIndex._normalize_filename(filename)
                            self.file_index[normalized_name] = file_path
                            self.original_names[normalized_name] = filename
                            total_files += 1
                except Exception as exc:
                    logger.error("无法访问目录 %s: %s", self.library_path, exc)

            logger.info("文件索引构建完成，共索引 %s 个 PDF 文件", total_files)
        except Exception as exc:
            logger.error("构建文件索引失败: %s", exc)

    def find_exact(self, filename: str) -> Optional[str]:
        """按文件名精确查找 PDF。"""
        if not filename:
            return None

        basename = os.path.basename(filename)
        normalized_name = FileIndex._normalize_filename(basename)
        return self.file_index.get(normalized_name)

    def find_fuzzy(self, keywords: List[str]) -> List[Tuple[str, str]]:
        """基于关键词模糊查找文件。"""
        results: List[Tuple[str, str]] = []
        for norm_name, file_path in self.file_index.items():
            match_count = sum(1 for kw in keywords if kw.lower() in norm_name)
            if match_count >= min(3, len(keywords)):
                results.append((self.original_names[norm_name], file_path))
        return results


def _is_translation(filename: str) -> bool:
    """判断文件是否为翻译版本。"""
    translation_keywords = ["中文翻译", "翻译版", "chinese translation", "译版"]
    filename_lower = filename.lower()
    return any(keyword.lower() in filename_lower for keyword in translation_keywords)


def _is_supplement(filename: str) -> bool:
    """判断文件是否为补充材料。"""
    supplement_keywords = [
        "supplementary material",
        "appendix",
        "SI.pdf",
        "supporting information",
        "supplement.pdf",
    ]
    filename_lower = filename.lower()
    return any(keyword.lower() in filename_lower for keyword in supplement_keywords)


def _score_pdf_quality(file_path: str, filename: str, title: str = "") -> Tuple[float, str]:
    """对候选 PDF 做轻量质量评分。"""
    score = 100.0
    diagnostics: List[str] = []

    try:
        file_size = os.path.getsize(file_path) / 1024
        if file_size < 1:
            score = 0.0
            diagnostics.append(f"文件过小({int(file_size)}KB)")
        elif file_size < 10:
            score -= 5.0
            diagnostics.append(f"文件较小({int(file_size)}KB)")
    except OSError:
        pass

    if _is_translation(filename):
        score -= 10.0
        diagnostics.append("或为翻译版本")

    if _is_supplement(filename):
        score -= 30.0
        diagnostics.append("或为补充材料")

    if title:
        title_words = set(re.findall(r"\w+", title.lower()))
        filename_words = set(re.findall(r"\w+", filename.lower()))
        if title_words:
            match_ratio = len(title_words & filename_words) / len(title_words)
            if match_ratio > 0.5:
                diagnostics.append("文件名匹配良好")
            elif match_ratio > 0.3:
                diagnostics.append("文件名部分匹配")

    return max(0.0, score), "; ".join(diagnostics) if diagnostics else "质量良好"


def find_pdf(
    paper_meta: Dict[str, Any],
    library_path: str,
    file_index: Optional[FileIndex] = None,
) -> Optional[str]:
    """
    智能 PDF 文件查找器。

    两步流程：
    1. 广泛搜索所有候选文件，包括精确匹配和标题模糊匹配
    2. 对候选结果进行轻量质量评分，选择最佳文件
    """
    if file_index is None:
        file_index = FileIndex(library_path)

    attachments: List[str] = paper_meta.get("attachments", [])
    title: str = paper_meta.get("title", "")
    candidates: List[Tuple[str, str]] = []

    if attachments:
        target_filenames: List[str] = []
        for attachment in attachments:
            filename = os.path.basename(attachment)
            if filename.startswith("o "):
                filename = filename[2:]
            target_filenames.append(filename)
            if not filename.lower().endswith(".pdf"):
                target_filenames.append(f"{filename}.pdf")

        if not target_filenames:
            logger.warning("附件列表处理后为空，转为标题匹配")
        else:
            logger.info("开始智能 PDF 查找，候选文件: %s", target_filenames[:5])
            for filename in target_filenames:
                matched_path = file_index.find_exact(filename)
                if matched_path:
                    candidates.append((filename, matched_path))

    if not candidates:
        if attachments:
            logger.info("精确匹配失败，尝试基于标题的模糊匹配...")
        else:
            logger.info("[INFO] 附件列表为空，直接尝试基于标题的模糊匹配...")

        if title:
            keywords = [word for word in re.findall(r"\w+", title) if len(word) > 3][:10]
            fuzzy_results = file_index.find_fuzzy(keywords)
            if fuzzy_results:
                logger.info("模糊匹配找到 %s 个候选文件", len(fuzzy_results))
                candidates.extend(fuzzy_results)
        elif not attachments:
            logger.error("元数据中既无附件也无标题，无法查找文件")
            return None

    if not candidates:
        logger.error("文件系统中找不到任何匹配的 PDF 文件。")
        return None

    logger.info("找到 %s 个候选 PDF 文件", len(candidates))

    scored_candidates: List[Tuple[float, str, str, str]] = []
    for filename, file_path in candidates:
        score, diagnostics = _score_pdf_quality(file_path, filename, title)
        scored_candidates.append((score, file_path, filename, diagnostics))
        logger.info("候选文件: %s", filename)
        logger.info("  - 质量分数: %.1f/100", score)
        logger.info("  - 诊断: %s", diagnostics)

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_path, best_filename, best_diagnostics = scored_candidates[0]

    if best_score <= 0:
        logger.error(
            "所有候选文件质量分数过低(最高 %.1f/100)。最佳候选: %s, 问题: %s",
            best_score,
            best_filename,
            best_diagnostics,
        )
        return None

    logger.info(
        "选择最佳 PDF: %s (分数: %.1f/100, %s)",
        best_filename,
        best_score,
        best_diagnostics,
    )
    logger.info("文件路径: %s", best_path)
    return best_path


def create_file_index(library_path: str) -> FileIndex:
    """创建文件索引实例。"""
    if not library_path:
        raise ValueError("library_path 必须是非空字符串")

    library_path = os.path.normpath(library_path)

    if not os.path.exists(library_path):
        raise OSError(f"Zotero 存储路径不存在: {library_path}")

    if not os.path.isdir(library_path):
        raise OSError(f"Zotero 存储路径不是目录: {library_path}")

    try:
        test_file = os.path.join(library_path, ".access_test")
        with open(test_file, "w", encoding="utf-8") as handle:
            handle.write("test")
        os.remove(test_file)
    except Exception as exc:
        raise OSError(f"无法写入 Zotero 存储目录，请检查权限: {exc}") from exc

    return FileIndex(library_path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2:
        library_path = sys.argv[1]
        filename = sys.argv[2]

        index = create_file_index(library_path)
        result = find_pdf({"attachments": [filename]}, library_path, index)

        if result:
            logger.info("找到文件: %s", result)
        else:
            logger.info("未找到文件")
    else:
        logger.info("使用方法: python file_finder.py <library_path> <filename>")
