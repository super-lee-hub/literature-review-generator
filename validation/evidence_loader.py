from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
import os
import json

@dataclass(frozen=True)
class PreprocessEvidence:
    """预处理证据对象，包含所有可用的证据来源"""
    normalized_text: str = ""
    plain_text: str = ""
    page_index: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    structured_json: Dict[str, Any] = field(default_factory=dict)
    manifest: Dict[str, Any] = field(default_factory=dict)
    visual_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class PreprocessEvidenceLoader:
    """预处理证据加载器，从磁盘加载各种预处理产物"""
    
    def load_evidence(
        self,
        plain_text_path: Optional[str] = None,
        page_index_path: Optional[str] = None,
        chunks_path: Optional[str] = None,
        structured_json_path: Optional[str] = None,
        manifest_path: Optional[str] = None,
        visual_artifacts_path: Optional[str] = None,
        diagnostics_path: Optional[str] = None,
    ) -> PreprocessEvidence:
        """加载预处理证据
        
        Args:
            plain_text_path: 纯文本文件路径
            page_index_path: 页面索引文件路径
            chunks_path: 分块文件路径
            structured_json_path: 结构化JSON文件路径
            manifest_path: 清单文件路径
            visual_artifacts_path: 视觉产物路径
            diagnostics_path: 诊断文件路径
            
        Returns:
            PreprocessEvidence: 加载的证据对象
        """
        normalized_text = self._load_text(plain_text_path)
        plain_text = normalized_text  # 默认为相同内容
        page_index = self._load_json(page_index_path, default=[])
        chunks = self._load_json(chunks_path, default=[])
        structured_json = self._load_json(structured_json_path, default={})
        manifest = self._load_json(manifest_path, default={})
        visual_artifacts = self._load_json(visual_artifacts_path, default=[])
        diagnostics = self._load_json(diagnostics_path, default={})
        
        return PreprocessEvidence(
            normalized_text=normalized_text,
            plain_text=plain_text,
            page_index=page_index,
            chunks=chunks,
            structured_json=structured_json,
            manifest=manifest,
            visual_artifacts=visual_artifacts,
            diagnostics=diagnostics,
        )
    
    def _load_text(self, path: Optional[str]) -> str:
        """加载文本文件"""
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return ""
    
    def _load_json(self, path: Optional[str], default: Any) -> Any:
        """加载JSON文件"""
        if not path or not os.path.exists(path):
            return default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default


def build_evidence_context_from_preprocess(
    evidence: PreprocessEvidence,
    paper_artifact: Dict[str, Any],
) -> Dict[str, Any]:
    """从预处理证据构建证据上下文
    
    Args:
        evidence: 预处理证据对象
        paper_artifact: 论文产物
        
    Returns:
        Dict[str, Any]: 证据上下文
    """
    return {
        "normalized_text": evidence.normalized_text,
        "plain_text": evidence.plain_text,
        "page_index": evidence.page_index,
        "chunks": evidence.chunks,
        "structured_json": evidence.structured_json,
        "manifest": evidence.manifest,
        "visual_artifacts": evidence.visual_artifacts,
        "diagnostics": evidence.diagnostics,
        "paper_artifact": paper_artifact,
    }
