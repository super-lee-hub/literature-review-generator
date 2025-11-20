#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PDF文本提取器
支持多种PDF格式的文本提取，包括扫描版PDF的文字识别

作者: 文献综述自动生成器开发团队
版本: 1.0
"""

import os
import logging
from typing import Optional, Dict, Any as _Any

# 为PyMuPDF(fitz)类型创建类型别名
try:
    import fitz  # PyMuPDF  # type: ignore
    # 类型别名，用于提高类型检查的清晰度
    FitxDocument = fitz.Document  # type: ignore
    FitxPage = fitz.Page  # type: ignore
except ImportError:
    # 如果fitz未安装，创建占位符类型
    class FitxDocument:  # type: ignore
        pass
    class FitxPage:  # type: ignore
        pass

def extract_text_from_pdf(pdf_path: str) -> Optional[str]:
    """
    从PDF文件中提取文本
    
    Args:
        pdf_path: PDF文件路径
        
    Returns:
        提取的文本内容，失败返回None
    """
    if not pdf_path or not os.path.exists(pdf_path):
        logging.error(f"PDF文件不存在: {pdf_path}")
        return None
    
    text_content: str = ""
    
    # 尝试使用pdfplumber提取文本
    try:
        import pdfplumber  # type: ignore
        
        with pdfplumber.open(pdf_path) as pdf:
            logging.info(f"PDF文件包含 {len(pdf.pages)} 页")
            
            for page_num, page in enumerate(pdf.pages, 1):
                page_text: Optional[str] = page.extract_text()  # type: ignore
                if page_text:
                    text_content = f"{text_content}\n--- 第{page_num}页 ---\n{page_text.strip()}\n"  # type: ignore
                else:
                    logging.warning(f"第{page_num}页无法提取文本（可能是扫描版）")
        
        if text_content.strip():  # type: ignore
            logging.info(f"使用pdfplumber成功提取文本，共 {len(text_content)} 字符")
            return text_content  # type: ignore
        else:
            logging.warning("pdfplumber提取结果为空")
            
    except Exception as e:
        logging.warning(f"pdfplumber提取失败: {e}")
    
    # 尝试使用PyMuPDF提取文本
    try:
        import fitz  # PyMuPDF  # type: ignore
        
        doc: FitxDocument = fitz.open(pdf_path)  # type: ignore
        logging.info(f"PyMuPDF: PDF文件包含 {doc.page_count} 页")  # type: ignore
        
        for page_num in range(doc.page_count):  # type: ignore
            page: _Any = doc[page_num]  # type: ignore - 使用_Any避免类型冲突
            page_text: str = page.get_text()  # type: ignore
            if page_text:
                text_content = f"{text_content}\n--- 第{page_num+1}页 ---\n{page_text.strip()}\n"  # type: ignore
            else:
                logging.warning(f"PyMuPDF第{page_num+1}页无法提取文本")
        
        doc.close()  # type: ignore
        
        if text_content.strip():  # type: ignore
            logging.info(f"使用PyMuPDF成功提取文本，共 {len(text_content)} 字符")
            return text_content  # type: ignore
        else:
            logging.warning("PyMuPDF提取结果为空")
            
    except Exception as e:
        logging.warning(f"PyMuPDF提取失败: {e}")
    
    # 如果都失败了
    logging.error("所有PDF文本提取方法都失败了")
    return None

def get_pdf_info(pdf_path: str) -> Optional[Dict[str, _Any]]:
    """
    获取PDF文件的基本信息
    
    Args:
        pdf_path: PDF文件路径
        
    Returns:
        包含PDF信息的字典，失败返回None
    """
    if not pdf_path or not os.path.exists(pdf_path):
        return None
    
    try:
        import fitz  # PyMuPDF  # type: ignore
        
        doc: _Any = fitz.open(pdf_path)  # type: ignore - 使用_Any避免类型冲突
        metadata: Dict[str, _Any] = doc.metadata or {}  # type: ignore
        info: Dict[str, _Any] = {
            'page_count': doc.page_count,  # type: ignore
            'title': metadata.get('title', '') or '',  # type: ignore
            'author': metadata.get('author', '') or '',  # type: ignore
            'creator': metadata.get('creator', '') or '',  # type: ignore
            'producer': metadata.get('producer', '') or '',  # type: ignore
            'file_size': os.path.getsize(pdf_path)
        }
        doc.close()  # type: ignore
        
        return info
        
    except Exception as e:
        logging.error(f"获取PDF信息失败: {e}")
        return None

def is_scanned_pdf(pdf_path: str) -> bool:
    """
    判断是否为扫描版PDF（图片型PDF）
    
    Args:
        pdf_path: PDF文件路径
        
    Returns:
        如果是扫描版PDF返回True，否则返回False
    """
    try:
        import fitz  # PyMuPDF  # type: ignore
        
        doc: _Any = fitz.open(pdf_path)  # type: ignore - 使用_Any避免类型冲突
        
        for page_num in range(doc.page_count):  # type: ignore
            page: _Any = doc[page_num]  # type: ignore - 使用_Any避免类型冲突
            # 如果页面上没有文本，则可能是扫描版
            text: str = page.get_text()  # type: ignore
            if not text or len(text.strip()) < 10:  # type: ignore
                doc.close()  # type: ignore
                return True
        
        doc.close()  # type: ignore
        return False
        
    except Exception as e:
        logging.error(f"检查PDF类型失败: {e}")
        return False