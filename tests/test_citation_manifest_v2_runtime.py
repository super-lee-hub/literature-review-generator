"""测试 citation_manifest_v2 运行时逻辑"""

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, cast

import pytest

from main import LiteratureReviewGenerator
from services.artifact_registry import ArtifactRegistry
from services.config_compat import CompatConfigView
from services.job_workspace import JobWorkspace
from services.progress_state import ResumeStateReport


class MockJobWorkspace:
    """模拟 JobWorkspace 类"""
    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.job_id = 'test_job'
        self.project_name = 'test_project'
    
    def ensure_exists(self):
        os.makedirs(self.root_dir, exist_ok=True)
    
    def artifact_path(self, filename):
        return os.path.join(self.root_dir, filename)
    
    def report_path(self, filename):
        return os.path.join(self.root_dir, 'reports', filename)
    
    def checkpoint_path(self, filename):
        return os.path.join(self.root_dir, 'checkpoints', filename)


class MockArtifactRegistry:
    """模拟 ArtifactRegistry 类"""
    def __init__(self):
        self.registered_files = []
    
    def register_file(self, **kwargs):
        self.registered_files.append(kwargs)


class MockCompatConfigView:
    """模拟 CompatConfigView 类"""
    pass


class MockResumeStateReport:
    """模拟 ResumeStateReport 类"""
    pass


def test_citation_manifest_v2_generation():
    """测试 main.py 正常路径仍生成并注册 v2 manifest"""
    # 创建临时目录
    with tempfile.TemporaryDirectory() as temp_dir:
        # 创建模拟的 JobWorkspace 和 ArtifactRegistry
        workspace = MockJobWorkspace(temp_dir)
        workspace.ensure_exists()
        
        registry = MockArtifactRegistry()
        compat_config = MockCompatConfigView()
        resume_state_report = MockResumeStateReport()
        
        # 创建 LiteratureReviewGenerator 实例
        generator = LiteratureReviewGenerator(project_name='test_project')
        # 直接设置属性而不是调用 bind_job_workspace，避免类型错误
        generator.job_workspace = cast(JobWorkspace, workspace)
        generator.artifact_registry = cast(ArtifactRegistry, registry)
        generator.compat_config = cast(CompatConfigView, compat_config)
        generator.resume_state_report = cast(ResumeStateReport, resume_state_report)
        generator.project_name = 'test_project'
        generator.output_dir = temp_dir
        generator.summary_file = os.path.join(temp_dir, 'test_summaries.json')
        
        # 模拟 summaries 数据（包含 status 字段）
        generator.summaries = [
            {
                'status': 'success',
                'paper_info': {
                    'title': 'Test Paper 1',
                    'authors': ['Author A', 'Author B'],
                    'year': '2023'
                }
            }
        ]
        
        # 创建模拟的 review_draft_v2 文件
        review_draft_v2 = {
            'artifact_type': 'review_draft',
            'artifact_version': 'v2',
            'content': {
                'sections': [
                    {
                        'section_number': 1,
                        'section_title': 'Introduction',
                        'blocks': [
                            {
                                'block_id': 's1_b1',
                                'block_order': 1,
                                'text': 'This is a test paragraph with citation.',
                                'citations': [
                                    {
                                        'local_ref_id': 'cit1',
                                        'citation_token': '[[cite:test_paper_1]]',
                                        'paper_id': 'test_paper_1',
                                        'paper_key': 'test_paper_1'
                                    }
                                ]
                            }
                        ]
                    }
                ],
                'references': ['Author A, B. (2023). Test Paper 1. Journal of Testing.']
            }
        }
        
        # 写入 review_draft_v2 文件
        review_draft_path = os.path.join(temp_dir, 'review_drafts', 'test_project_review_draft_v2.json')
        os.makedirs(os.path.dirname(review_draft_path), exist_ok=True)
        with open(review_draft_path, 'w', encoding='utf-8') as f:
            json.dump(review_draft_v2, f)
        
        # 调用 _persist_citation_manifest
        result = generator._persist_citation_manifest(
            review_draft_path=review_draft_path,
            review_word_path=os.path.join(temp_dir, 'test_review.docx'),
            citations=[]  # 空 citations，应该使用 v2 路径
        )
        
        # 验证结果
        assert result == True
        
        # 验证 v2 manifest 文件已创建
        citation_manifest_path = os.path.join(temp_dir, 'citation_manifests', 'test_project_citation_manifest_v2.json')
        assert os.path.exists(citation_manifest_path)
        
        # 验证 v1 compatibility 文件已创建
        citation_manifest_v1_path = os.path.join(temp_dir, 'citation_manifests', 'test_project_citation_manifest_v1.json')
        assert os.path.exists(citation_manifest_v1_path)
        
        # 验证 artifact 已注册
        assert len(registry.registered_files) > 0
        v2_registration = None
        for registration in registry.registered_files:
            if registration.get('artifact_version') == 'v2':
                v2_registration = registration
                break
        assert v2_registration is not None
        assert v2_registration.get('artifact_type') == 'citation_manifest'


def test_citation_manifest_v1_fallback():
    """测试 review_draft_v2 不可加载时仍能走 v1 migration fallback"""
    # 创建临时目录
    with tempfile.TemporaryDirectory() as temp_dir:
        # 创建模拟的 JobWorkspace 和 ArtifactRegistry
        workspace = MockJobWorkspace(temp_dir)
        workspace.ensure_exists()
        
        registry = MockArtifactRegistry()
        compat_config = MockCompatConfigView()
        resume_state_report = MockResumeStateReport()
        
        # 创建 LiteratureReviewGenerator 实例
        generator = LiteratureReviewGenerator(project_name='test_project')
        # 直接设置属性而不是调用 bind_job_workspace，避免类型错误
        generator.job_workspace = cast(JobWorkspace, workspace)
        generator.artifact_registry = cast(ArtifactRegistry, registry)
        generator.compat_config = cast(CompatConfigView, compat_config)
        generator.resume_state_report = cast(ResumeStateReport, resume_state_report)
        generator.project_name = 'test_project'
        generator.output_dir = temp_dir
        generator.summary_file = os.path.join(temp_dir, 'test_summaries.json')
        
        # 模拟 summaries 数据（包含 status 字段）
        generator.summaries = [
            {
                'status': 'success',
                'paper_info': {
                    'title': 'Test Paper 1',
                    'authors': ['Author A', 'Author B'],
                    'year': '2023'
                }
            }
        ]
        
        # 使用不存在的 review_draft_v2 文件路径
        review_draft_path = os.path.join(temp_dir, 'non_existent_draft.json')
        
        # 准备 citations 数据用于 fallback
        citations = [
            {
                'citation_id': 'cit1',
                'paper_id': 'test_paper_1',
                'text': '(Author A, 2023)',
                'context': 'Test context',
                'section_number': 1,
                'section_title': 'Introduction',
                'block_id': 's1_b1',
                'block_order': 1
            }
        ]
        
        # 调用 _persist_citation_manifest
        result = generator._persist_citation_manifest(
            review_draft_path=review_draft_path,
            review_word_path=os.path.join(temp_dir, 'test_review.docx'),
            citations=citations  # 提供 citations 用于 fallback
        )
        
        # 验证结果
        assert result == True
        
        # 验证 v2 manifest 文件已创建（通过 migration）
        citation_manifest_path = os.path.join(temp_dir, 'citation_manifests', 'test_project_citation_manifest_v2.json')
        assert os.path.exists(citation_manifest_path)
        
        # 验证 v1 compatibility 文件已创建
        citation_manifest_v1_path = os.path.join(temp_dir, 'citation_manifests', 'test_project_citation_manifest_v1.json')
        assert os.path.exists(citation_manifest_v1_path)
