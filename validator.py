#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
验证与修正模块
负责对AI生成的内容进行交叉验证，确保准确性和可信度。
"""
import os
import json
import re
import traceback
from typing import Optional, Dict, Any, List
from datetime import datetime
import configparser

# 导入类型定义
from models import APIConfig  # type: ignore

# 优雅地处理可选依赖，确保模块的独立健壮性
try:
    from docx import Document  # type: ignore
    DOCX_AVAILABLE = True  # type: ignore
except ImportError:
    DOCX_AVAILABLE = False  # type: ignore
    Document = None  # type: ignore

try:
    from tqdm import tqdm  # type: ignore
    TQDM_AVAILABLE = True  # type: ignore
except ImportError:
    TQDM_AVAILABLE = False  # type: ignore
    from typing import Any, Optional, Iterator
    class tqdm:
        def __init__(self, iterable: Optional[Any] = None, **kwargs: Any):
            self.iterable: Any = iterable if iterable else []  # type: ignore
        def __iter__(self) -> Iterator[Any]:
            return iter(self.iterable)
        def set_postfix_str(self, s: str) -> None:
            pass

# 导入主程序中的AI接口调用函数
from ai_interface import _call_ai_api  # type: ignore
from summary_schema import get_core_analysis

def validate_paper_analysis(generator_instance: Any, pdf_text: str, ai_result: Dict[str, Any],
                           use_cache: bool = True) -> Dict[str, Any]:
    """
    [第一阶段验证] 对单篇论文的AI分析结果进行交叉验证和修正。
    增强异常处理和输入验证，支持验证结果缓存

    Args:
        generator_instance: 文献综述生成器实例
        pdf_text: PDF全文内容
        ai_result: AI分析结果
        use_cache: 是否使用验证结果缓存（提高性能）

    Returns:
        修正后的AI分析结果
    """
    # 输入验证
    if not pdf_text:
        generator_instance.logger.warning("PDF文本为空或无效，跳过验证")
        return ai_result

    if not ai_result:
        generator_instance.logger.warning("AI分析结果为空或无效，跳过验证")
        return ai_result

    # 生成内容哈希用于缓存
    content_hash: Optional[str] = None
    cache_file_path: Optional[str] = None
    if use_cache:
        import hashlib
        paper_info: Any = ai_result.get('paper_info') or {}  # type: ignore
        content_str = pdf_text[:1000] + str(paper_info.get('title', '')) + str(paper_info.get('authors', []))  # type: ignore
        content_hash = hashlib.md5(content_str.encode('utf-8')).hexdigest()

        # 构建缓存文件路径
        cache_dir = os.path.join(generator_instance.output_dir, 'cache')  # type: ignore
        try:
            os.makedirs(cache_dir, exist_ok=True)
            cache_file_path = os.path.join(cache_dir, f'{content_hash}.json')
        except Exception as _:  # type: ignore
            generator_instance.logger.warning(f"创建缓存目录失败: {_}，将跳过缓存")  # type: ignore
            cache_file_path = None

    # 检查缓存
    if use_cache and content_hash and cache_file_path and os.path.exists(cache_file_path):
        try:
            with open(cache_file_path, 'r', encoding='utf-8') as f:
                cached_result = json.load(f)
            generator_instance.logger.info("从缓存中加载验证结果")
            return cached_result
        except Exception as e:
            generator_instance.logger.warning(f"读取缓存文件失败: {e}，将重新验证")

    generator_instance.logger.info("启动第一阶段交叉验证...")

    # 预检查：如果摘要包含占位符'...'，跳过验证（因为验证AI会错误地填充它）
    try:
        common_core = get_core_analysis(ai_result)
        placeholder_fields: List[str] = []
        
        # 检查所有字段是否包含'...'
        for field, value in common_core.items():
            if isinstance(value, str) and '...' in value:
                placeholder_fields.append(field)
            elif isinstance(value, list):
                for i, item in enumerate(value):  # type: ignore
                    if isinstance(item, str) and '...' in item:
                        placeholder_fields.append(f"{field}[{i}]")
        
        if placeholder_fields:
            generator_instance.logger.warning(f"发现占位符'...'在字段: {', '.join(placeholder_fields)}，跳过验证以避免错误填充")
            generator_instance.logger.info("内容质量检查通过（跳过验证）")
            return ai_result
    except Exception as e:
        generator_instance.logger.warning(f"预检查占位符时出错: {e}，继续正常验证流程")

    try:
        # 安全获取配置
        validator_config: Dict[str, str] = generator_instance.config.get('Validator_API', {})
        if not validator_config:
            generator_instance.logger.error("未找到[Validator_API]配置段，跳过验证。")  # type: ignore
            return ai_result

        validator_api_config: Dict[str, str] = {  # type: ignore
            'api_key': validator_config.get('api_key', ''),  # type: ignore
            'model': validator_config.get('model', ''),  # type: ignore
            'api_base': validator_config.get('api_base', 'https://api.openai.com/v1')  # type: ignore
        }  # type: ignore

        # 验证配置完整性
        if not validator_api_config['api_key'] or not validator_api_config['api_key'].strip():
            generator_instance.logger.error("Validator_API的api_key未配置或为空，跳过验证。")
            return ai_result

        if not validator_api_config['model'] or not validator_api_config['model'].strip():
            generator_instance.logger.error("Validator_API的model未配置或为空，跳过验证。")
            return ai_result

        # 使用严格验证提示词，只检查客观事实错误
        prompt_file_path: str = 'prompts/prompt_validate_analysis_strict.txt'
        try:
            with open(prompt_file_path, 'r', encoding='utf-8') as f:
                prompt_template = f.read()
        except FileNotFoundError:
            generator_instance.logger.error(f"提示词文件不存在: {prompt_file_path}，跳过验证。")
            return ai_result
        except UnicodeDecodeError:
            generator_instance.logger.error(f"提示词文件编码错误: {prompt_file_path}，跳过验证。")
            return ai_result
        except Exception as e:
            generator_instance.logger.error(f"读取提示词文件失败: {e}，跳过验证。")
            return ai_result

        # 安全生成提示词
        try:
            summary_str: str = json.dumps(ai_result, ensure_ascii=False, indent=2)
            max_text_len: int = 800000  # 限制文本长度，防止API调用超限

            # 截断过长的文本
            truncated_pdf_text = pdf_text[:max_text_len] if len(pdf_text) > max_text_len else pdf_text

            final_prompt = prompt_template.replace('{{PAPER_FULL_TEXT}}', truncated_pdf_text)
            final_prompt = final_prompt.replace('{{GENERATED_SUMMARY}}', summary_str)
        except Exception as e:
            generator_instance.logger.error(f"生成验证提示词失败: {e}，跳过验证。")
            return ai_result

        system_prompt = "你是一位严谨的学术事实核查员。你的任务是对比论文原文和AI生成的摘要，找出并修正摘要中的任何不准确之处。"

        # 调用验证API
        try:
            # 从配置中读取API参数
            validator_max_tokens: int = int((generator_instance.config.get('API_Parameters') or {}).get('validator_max_tokens', 4096))  # type: ignore
            validator_temperature: float = float((generator_instance.config.get('API_Parameters') or {}).get('validator_temperature', 0.3))  # type: ignore

            validation_report = _call_ai_api(
                final_prompt,
                validator_api_config,  # type: ignore
                system_prompt,
                max_tokens=validator_max_tokens,
                temperature=validator_temperature,
                response_format="json",
                logger=generator_instance.logger  # type: ignore
            )  # type: ignore
        except Exception as e:
            generator_instance.logger.error(f"调用验证API失败: {e}，跳过验证。")
            return ai_result

        # 处理验证结果
        if not validation_report:
            generator_instance.logger.error("验证过程返回空报告，将使用未经核实的摘要。")
            return ai_result

        if not validation_report:
            generator_instance.logger.error("验证报告格式无效，将使用未经核实的摘要。")
            return ai_result

        # 检查一致性并应用修正
        is_consistent: bool = validation_report.get("is_consistent", True)
        if not is_consistent:
            feedback: str = validation_report.get('feedback', '无反馈信息')
            generator_instance.logger.warn(f"验证发现不一致: {feedback}")

            corrections: List[Dict[str, Any]] = validation_report.get("corrections", [])
            if not corrections:
                generator_instance.logger.info("报告存在不一致，但未提供具体修正项。")
                return ai_result

            # 🆕 智能应用修正：引入"智能追加"策略
            applied_corrections: int = 0
            for i, correction in enumerate(corrections, 1):
                try:
                    if not correction:
                        generator_instance.logger.warning(f"修正项{i}格式无效，跳过")
                        continue

                    field_to_correct = correction.get("field")
                    corrected_value = correction.get("corrected_value")

                    if not field_to_correct or not isinstance(field_to_correct, str):
                        generator_instance.logger.warning(f"修正项{i}缺少字段名或字段名无效，跳过")
                        continue

                    if corrected_value is None:
                        generator_instance.logger.warning(f"修正项{i}缺少修正值，跳过")
                        continue
                    
                    # 检查修正值的有效性
                    if isinstance(corrected_value, str) and corrected_value.strip() == '':
                        generator_instance.logger.warning(f"修正项{i}修正值为空字符串，跳过")
                        continue
                    
                    if isinstance(corrected_value, str) and len(corrected_value.strip()) < 3:
                        generator_instance.logger.warning(f"修正项{i}修正值过短({len(corrected_value.strip())}字符): '{corrected_value}'，跳过")
                        continue

                    # 导航到目标位置
                    keys: List[str] = field_to_correct.split('.')
                    temp_dict: Dict[str, Any] = ai_result

                    # 安全导航到目标位置
                    for key in keys[:-1]:
                        if key not in temp_dict:
                            temp_dict[key] = {}
                        elif not isinstance(temp_dict[key], dict):
                            generator_instance.logger.warning(f"修正项{i}的目标路径 '{field_to_correct}' 包含非字典类型，跳过")
                            break
                        temp_dict = temp_dict[key]
                    else:
                        field_name = keys[-1]
                        original_value = temp_dict.get(field_name, '')
                        
                        # 记录修正前状态
                        generator_instance.logger.info(f"🔍 修正前: {field_to_correct} = '{str(original_value)[:100]}...' (长度: {len(str(original_value))})")
                        generator_instance.logger.info(f"🔍 修正值: '{str(corrected_value)[:100]}...' (长度: {len(str(corrected_value))})")
                        
                        # 🎯 智能分支处理策略
                        is_original_empty = (not original_value or 
                                           original_value in ['未提供相关信息', '未提及', '', 'N/A', '...'])
                        is_corrected_valid = (corrected_value and 
                                             corrected_value not in ['未提供相关信息', '未提及', '', 'N/A'])
                        
                        if isinstance(original_value, str) and isinstance(corrected_value, str):
                            original_len = len(original_value)
                            corrected_len = len(corrected_value)
                            
                            # 情况A：完全替换 - 修正值长度显著大于原值（>80%），或者原值为空/占位符
                            # 提高阈值从0.6到0.8，避免过短修正导致信息丢失
                            if is_original_empty or corrected_len > original_len * 0.8:
                                temp_dict[field_name] = corrected_value
                                generator_instance.logger.info(f"✅ 字段 '{field_to_correct}' 执行完全替换 (修正长度: {corrected_len}, 原长度: {original_len})")
                                
                            # 情况B：精准替换 - 修正值较短，直接替换（不再追加验证元数据）
                            else:
                                # 直接使用修正值替换原值，避免验证元数据污染摘要
                                temp_dict[field_name] = corrected_value
                                # 记录修正依据供调试参考（不存储到摘要中）
                                justification = ""
                                for correction in corrections:
                                    if correction.get("field") == field_to_correct:
                                        justification = correction.get("justification", "")
                                        break
                                if justification:
                                    generator_instance.logger.debug(f"🔧 修正依据: {justification}")
                                generator_instance.logger.info(f"✅ 字段 '{field_to_correct}' 执行精准替换 (修正: {corrected_len}字符替换原值: {original_len}字符)")
                                
                        elif is_corrected_valid:
                            # 非字符串类型修正，直接替换
                            temp_dict[field_name] = corrected_value
                            generator_instance.logger.info(f"✅ 字段 '{field_to_correct}' 已替换修正信息 (非字符串类型)")
                        else:
                            # 修正值无效，保持原值
                            generator_instance.logger.warning(f"⚠️  字段 '{field_to_correct}' 保持原值 (修正值无效)")
                        
                        # 记录修正后状态
                        final_value = temp_dict.get(field_name, '')
                        generator_instance.logger.info(f"🔍 修正后: {field_to_correct} = '{str(final_value)[:100]}...' (长度: {len(str(final_value))})")
                        
                        applied_corrections += 1

                except Exception as e:
                    generator_instance.logger.error(f"应用修正项{i}时出错: {e}")
                    continue

            generator_instance.logger.info(f"共应用了 {applied_corrections}/{len(corrections)} 个修正项")

        else:
            generator_instance.logger.success("验证通过，分析内容与原文一致。")

    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        generator_instance.logger.error(f"配置文件错误: {e}，跳过验证。请检查config.ini。")
    except Exception as e:
        generator_instance.logger.error(f"验证模块发生未知异常: {e}")
        generator_instance.logger.debug(f"详细错误信息: {traceback.format_exc()}")

    # 保存验证结果到缓存
    if use_cache and content_hash and cache_file_path and ai_result:
        try:
            with open(cache_file_path, 'w', encoding='utf-8') as f:
                json.dump(ai_result, f, ensure_ascii=False, indent=2)
            generator_instance.logger.debug(f"验证结果已保存到缓存: {cache_file_path}")
        except Exception as e:
            generator_instance.logger.warning(f"保存缓存文件失败: {e}")

    return ai_result

def _validate_claims_for_single_paper(source_summary: dict, sentences: List[str], api_config: dict, config: dict = None) -> Optional[dict]:  # type: ignore
    """为单篇论文的所有引用句子调用一次AI进行批量验证"""
    try:
        # 读取API参数配置
        try:
            if config:
                max_tokens: int = int(config.get('API_Parameters', {}).get('claims_max_tokens', 8192))  # type: ignore
                temperature: float = float(config.get('API_Parameters', {}).get('claims_temperature', 0.3))  # type: ignore
            else:
                max_tokens = 8192
                temperature = 0.3
        except (ValueError, TypeError) as _:  # type: ignore
            max_tokens = 8192
            temperature = 0.3

        with open('prompts/prompt_validate_claims_batch.txt', 'r', encoding='utf-8') as f:
            prompt_template: str = f.read()

        summary_str: str = json.dumps(source_summary, ensure_ascii=False, indent=2)
        sentences_str: str = json.dumps(sentences, ensure_ascii=False, indent=2)

        final_prompt = prompt_template.replace('{{SOURCE_SUMMARY}}', summary_str)
        final_prompt = final_prompt.replace('{{SENTENCES_TO_VALIDATE}}', sentences_str)

        system_prompt = "你是一位严谨的学术编辑，负责批量核查文稿中引用的准确性。你的任务是判断一个句子列表中的每句话是否都得到了其引用的文献摘要的支持。"

        return _call_ai_api(final_prompt, api_config, system_prompt, max_tokens=max_tokens, temperature=temperature, response_format="json")  # type: ignore

    except Exception as _:  # type: ignore
        # 使用generator_instance的logger，如果可用
        # 注意：这里没有generator_instance的引用，所以暂时不记录日志
        return None

def run_review_validation(generator_instance: Any) -> dict:  # type: ignore
    """
    [第二阶段验证] 对生成的文献综述进行高效、批量的验证（使用工件而非docx）。
    """
    generator_instance.logger.info("=" * 60 + "\n文献综述验证阶段 (高效版)\n" + "=" * 60)  # type: ignore
    try:
        if not generator_instance.config.getboolean('Performance', 'enable_stage2_validation', fallback=False):  # type: ignore
            generator_instance.logger.warn("第二阶段验证未在配置中启用。跳过此步骤。")  # type: ignore
            return {"success": True, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}

        # 尝试使用Week 3验证流程（基于工件）
        try:
            # 加载review_draft_v2
            review_draft_path = generator_instance._review_draft_v2_path()
            if not os.path.exists(review_draft_path):
                generator_instance.logger.error(f"找不到review_draft_v2文件: {review_draft_path}。请先生成综述。")
                return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
            
            with open(review_draft_path, 'r', encoding='utf-8') as f:
                review_draft = json.load(f)
            
            # 加载citation_manifest
            citation_manifest_path = generator_instance._citation_manifest_path()
            if not os.path.exists(citation_manifest_path):
                generator_instance.logger.error(f"找不到citation_manifest文件: {citation_manifest_path}。请先生成综述。")
                return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
            
            with open(citation_manifest_path, 'r', encoding='utf-8') as f:
                citation_manifest = json.load(f)
            
            # 优先从 workspace / artifact registry 加载持久化 artifact
            paper_artifacts = []
            try:
                # 尝试从 artifact registry 加载
                from services.artifact_registry import ArtifactRegistry
                from services.job_workspace import JobWorkspace
                
                # 构建 workspace 和 registry
                if hasattr(generator_instance, 'job_workspace') and generator_instance.job_workspace:
                    workspace = generator_instance.job_workspace
                    job_id = workspace.job_id
                    registry = generator_instance.artifact_registry or ArtifactRegistry(workspace.paths.registry_path, job_id)
                else:
                    # 回退到使用 project_name 和生成的 job_id
                    project_name = generator_instance.project_name or "unknown_project"
                    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
                    workspace = JobWorkspace(generator_instance.output_dir, project_name, job_id)
                    registry = ArtifactRegistry(workspace.paths.registry_path, job_id)
                
                # 加载 paper artifacts
                for record in registry.list_records():
                    if record.artifact_type == "paper_artifact" and record.status == "ready":
                        try:
                            with open(record.path, 'r', encoding='utf-8') as f:
                                paper_artifact = json.load(f)
                            paper_artifacts.append(paper_artifact)
                        except Exception as e:
                            generator_instance.logger.warning(f"加载 paper artifact 失败: {e}")
                
                # 如果没有从 registry 加载到，回退到从 summaries 构建
                if not paper_artifacts:
                    generator_instance.logger.info("从 artifact registry 加载失败，回退到从 summaries 构建 paper_artifacts")
                    for summary in generator_instance.summaries:
                        paper_info = summary.get('paper_info', {})
                        ai_summary = summary.get('ai_summary', {})
                        
                        paper_artifact = {
                            "paper_identity": {
                                "canonical_paper_key": generator_instance.get_paper_key(paper_info),
                                "source_paper_id": paper_info.get('pdf_path', '')
                            },
                            "analysis": {
                                "ai_summary": ai_summary
                            },
                            "source": {
                                "source_pdf": paper_info.get('pdf_path', '')
                            }
                        }
                        paper_artifacts.append(paper_artifact)
            except Exception as e:
                generator_instance.logger.warning(f"从 artifact registry 加载失败: {e}，回退到从 summaries 构建")
                # 回退到从 summaries 构建
                for summary in generator_instance.summaries:
                    paper_info = summary.get('paper_info', {})
                    ai_summary = summary.get('ai_summary', {})
                    
                    paper_artifact = {
                        "paper_identity": {
                            "canonical_paper_key": generator_instance.get_paper_key(paper_info),
                            "source_paper_id": paper_info.get('pdf_path', '')
                        },
                        "analysis": {
                            "ai_summary": ai_summary
                        },
                        "source": {
                            "source_pdf": paper_info.get('pdf_path', '')
                        }
                    }
                    paper_artifacts.append(paper_artifact)
            
            # 构建 preprocess_evidence 和 paper_metadata
            preprocess_evidence = {}
            paper_metadata = {}
            
            # 从 paper_artifacts 中提取相关信息
            for artifact in paper_artifacts:
                paper_key = artifact.get('paper_identity', {}).get('canonical_paper_key', '')
                if paper_key:
                    # 提取 preprocess evidence
                    preprocess_evidence[paper_key] = artifact.get('stage1_inputs', {}).get('preprocess_evidence', {})
                    # 提取 paper metadata
                    paper_metadata[paper_key] = artifact.get('paper_identity', {})
            
            # 运行Week 3验证
            from validation.review_validator import ReviewValidator
            validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata)
            report = validator.validate()
            
            # 运行summary_recheck
            from validation.summary_recheck import run_summary_rechecks
            recheck_reports = run_summary_rechecks(paper_artifacts)
            
            # 生成验证报告
            report_lines = ["llm_reviewer_generator文献综述验证报告", f"生成时间: {datetime.now().isoformat()}\n", "="*30]
            report_lines.append(f"【验证摘要】")
            report_lines.append(f"总引用数: {report.total_citations}")
            report_lines.append(f"支持: {report.supported_count}")
            report_lines.append(f"部分支持: {report.partial_support_count}")
            report_lines.append(f"不支持: {report.unsupported_count}")
            report_lines.append(f"错误来源: {report.wrong_source_count}")
            report_lines.append(f"需要审核: {report.needs_review_count}")
            
            # 添加summary_recheck结果
            if recheck_reports:
                report_lines.append("\n【摘要检查结果】")
                report_lines.append(f"检查论文数: {len(recheck_reports)}")
                total_candidates = sum(len(report.correction_candidates) for report in recheck_reports)
                report_lines.append(f"修正建议数: {total_candidates}")
                
                for i, recheck_report in enumerate(recheck_reports, 1):
                    if recheck_report.correction_candidates:
                        report_lines.append(f"\n{i}. 论文: {recheck_report.paper_key}")
                        for j, candidate in enumerate(recheck_report.correction_candidates, 1):
                            report_lines.append(f"   {j}. 字段: {candidate.field_path}")
                            report_lines.append(f"      原因: {candidate.reason}")
            
            if report.citation_results:
                report_lines.append("\n【详细结果】")
                for i, result in enumerate(report.citation_results, 1):
                    report_lines.append(f"\n{i}. 引用ID: {result.citation_id}")
                    report_lines.append(f"   论文ID: {result.paper_id}")
                    report_lines.append(f"   结论: {result.conclusion.value}")
                    report_lines.append(f"   根本原因: {[rc.value for rc in result.root_causes]}")
                    report_lines.append(f"   证据候选数: {len(result.evidence_candidates)}")
            
            # 保存报告到 workspace 目录
            try:
                from services.job_workspace import JobWorkspace
                # 使用 project_name 和生成的 job_id
                project_name = generator_instance.project_name or "unknown_project"
                job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
                workspace = JobWorkspace(generator_instance.output_dir, project_name, job_id)
                report_file: str = os.path.join(workspace.paths.reports_dir, f'{project_name}_validation_report.txt')
                # 确保目录存在
                os.makedirs(os.path.dirname(report_file), exist_ok=True)
            except Exception as e:
                generator_instance.logger.warning(f"创建 workspace 目录失败: {e}，回退到旧路径")
                report_file: str = os.path.join(generator_instance.output_dir, f'{generator_instance.project_name}_validation_report.txt')
            
            with open(report_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            
            generator_instance.logger.info(f"验证报告已保存到: {report_file}")
            generator_instance.logger.success("Week 3验证流程完成！")
            return {"success": True, "report": report, "review_draft": review_draft, "citation_manifest": citation_manifest, "paper_artifacts": paper_artifacts}
            
        except Exception as e:
            generator_instance.logger.warning(f"Week 3验证流程失败，回退到传统验证: {e}")
            # 继续执行传统验证流程（docx-based）
            pass

        # 传统验证流程（回退）
        if not DOCX_AVAILABLE:
            generator_instance.logger.error("python-docx模块未安装，无法进行第二阶段验证。请运行: pip install python-docx")  # type: ignore
            return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}

        word_file: str = os.path.join(generator_instance.output_dir, f'{generator_instance.project_name}_literature_review.docx')  # type: ignore
        if not os.path.exists(word_file):
            generator_instance.logger.error(f"找不到文献综述文件: {word_file}。请先生成综述。")  # type: ignore
            return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
            
        validator_api_config: Dict[str, str] = {
            'api_key': (generator_instance.config.get('Validator_API') or {}).get('api_key', ''),  # type: ignore
            'model': (generator_instance.config.get('Validator_API') or {}).get('model', ''),  # type: ignore
            'api_base': (generator_instance.config.get('Validator_API') or {}).get('api_base', 'https://api.openai.com/v1')  # type: ignore
        }
        api_config_valid: bool = bool(validator_api_config['api_key'] and validator_api_config['model'])  # type: ignore

        doc = Document(word_file)  # type: ignore
        
        # --- 1. 建立文献库索引和引用索引 ---
        generator_instance.logger.info("步骤1/3: 正在索引文献库和综述中的所有引用...")
        valid_citation_map: Dict[str, Dict[str, Any]] = {} # {'(Author, YYYY)': summary}
        citation_to_key: Dict[str, str] = {}    # {'(Author et al., YYYY)': '(Author, YYYY)'}
        for i, summary in enumerate(generator_instance.summaries):  # type: ignore
            info: Dict[str, Any] = summary.get('paper_info', {})
            authors: List[str] = info.get('authors', [])
            year: str = str(info.get('year', 'N/A'))
            if authors and year != 'N/A':
                # 创建标准引用格式 (Author, YYYY)
                if len(authors) == 1:
                    standard_citation: str = f"({authors[0]}, {year})"
                elif len(authors) <= 3:
                    standard_citation: str = f"({', '.join(authors[:-1])} & {authors[-1]}, {year})"
                else:
                    standard_citation: str = f"({authors[0]} et al., {year})"
                
                valid_citation_map[standard_citation] = summary
                
                # 创建多种引用格式的映射，支持中文和英文格式变体
                # 首先定义标准化函数（局部使用）
                def normalize_citation_for_mapping(citation: str) -> str:
                    """标准化引用字符串用于映射键，与normalize_citation保持相同逻辑"""
                    if not citation:
                        return citation
                    
                    # 1. 移除多余空格，将多个空格合并为一个
                    citation = re.sub(r'\s+', ' ', citation).strip()
                    
                    # 2. 统一标点：中文标点替换为英文标点
                    citation = citation.replace('；', ';').replace('，', ',').replace('、', ',')
                    
                    # 3. 移除常见的中文前缀
                    citation = re.sub(r'\(支持文献[:：]\s*', '(', citation)
                    citation = re.sub(r'\(参见[:：]\s*', '(', citation)
                    citation = re.sub(r'\(来源[:：]\s*', '(', citation)
                    citation = re.sub(r'\(引用自[:：]\s*', '(', citation)
                    
                    # 4. 统一"和"与"&" - 使用与normalize_citation相同的安全替换
                    citation = re.sub(r'([a-zA-Z\u4e00-\u9fff]+)\s+和\s+([a-zA-Z\u4e00-\u9fff]+)', r'\1 & \2', citation)
                    citation = re.sub(r'([a-zA-Z\u4e00-\u9fff]+)和([a-zA-Z\u4e00-\u9fff]+)', r'\1 & \2', citation)
                    
                    # 5. 统一"等"与"et al." - 与normalize_citation保持一致
                    citation = re.sub(r'等\s*,', ' et al.,', citation)
                    citation = re.sub(r'等\s*;', ' et al.;', citation)
                    citation = re.sub(r'等\s*\)', ' et al.)', citation)
                    citation = re.sub(r'\s等\s*,', ' et al.,', citation)
                    citation = re.sub(r'等\s+', ' et al. ', citation)
                    
                    # 6. 确保年份前有空格，同时处理年份后的空格
                    citation = re.sub(r',(\d{4})', r', \1', citation)
                    citation = re.sub(r',\s+(\d{4})', r', \1', citation)  # 多个空格变体
                    citation = re.sub(r',\s*(\d{4})\s*\)', r', \1)', citation)  # 年份后空格
                    
                    # 7. 规范化作者分隔符和空格
                    citation = re.sub(r'\(\s*', '(', citation)
                    citation = re.sub(r'\s*\)', ')', citation)
                    citation = re.sub(r'\s*,\s*', ', ', citation)
                    citation = re.sub(r'\s*&\s*', ' & ', citation)
                    
                    # 7.5 处理逗号+&格式（如"作者1, & 作者2"）
                    citation = re.sub(r',\s*&', ' &', citation)
                    
                    # 8. 清理可能产生的双逗号和多余空格
                    citation = re.sub(r',\s*,', ', ', citation)
                    citation = re.sub(r'et al\.\s*,', 'et al.,', citation)
                    citation = re.sub(r'\s+', ' ', citation)
                    
                    # 8.5 最终空格规范化
                    citation = citation.replace('( ', '(').replace(' )', ')')
                    citation = re.sub(r'\s+', ' ', citation)  # 最终空格合并
                    
                    return citation
                
                # 生成所有可能的引用格式变体
                citation_variants: List[str] = []
                
                if len(authors) == 1:
                    # 单作者变体（包括AI可能错误生成的'等'格式）
                    base_formats: List[str] = [
                        # 标准格式
                        f"({authors[0]}, {year})",
                        f"({authors[0]},  {year})",  # 双空格
                        f"({authors[0]},{year})",    # 无空格
                        # 等格式变体
                        f"({authors[0]} 等, {year})",
                        f"({authors[0]}等, {year})",
                        f"({authors[0]}等, {year})",
                        f"({authors[0]} 等,{year})",
                        f"({authors[0]}等,{year})",
                        # et al.格式变体
                        f"({authors[0]} et al., {year})",
                        f"({authors[0]}et al., {year})",
                        f"({authors[0]} et al.,{year})",
                        f"({authors[0]}et al.,{year})",
                        # 其他可能变体
                        f"({authors[0]}, {year})",  # 原始格式重复
                        f"({authors[0]}, {year})",  # 无空格变体
                    ]
                    citation_variants.extend(base_formats)
                    
                elif len(authors) == 2:
                    # 双作者变体
                    base_formats: List[str] = [
                        f"({authors[0]} & {authors[1]}, {year})",
                        f"({authors[0]}&{authors[1]}, {year})",
                        f"({authors[0]} &{authors[1]}, {year})",
                        f"({authors[0]}& {authors[1]}, {year})",
                        f"({authors[0]}, {authors[1]}, {year})",
                        f"({authors[0]}, {authors[1]}, {year})",  # 带空格变体
                        f"({authors[0]},{authors[1]}, {year})",   # 无空格变体
                        f"({authors[0]} 和 {authors[1]}, {year})",
                        f"({authors[0]}和 {authors[1]}, {year})",
                        f"({authors[0]}和{authors[1]}, {year})",
                        f"({authors[0]} 和{authors[1]}, {year})",
                        f"({authors[0]}、{authors[1]}, {year})",
                        f"({authors[0]}、{authors[1]}, {year})",  # 中文顿号
                        f"({authors[0]} 等, {year})",          # 中文等格式
                        f"({authors[0]}等, {year})",           # 中文等格式（无空格）
                        f"({authors[0]}等, {year})",           # 中文等格式（紧贴）
                        f"({authors[0]} et al., {year})",
                        f"({authors[0]}et al., {year})"
                    ]
                    citation_variants.extend(base_formats)
                    
                elif len(authors) == 3:
                    # 三作者变体
                    base_formats: List[str] = [
                        # 英文格式变体
                        f"({authors[0]}, {authors[1]} & {authors[2]}, {year})",
                        f"({authors[0]},{authors[1]} & {authors[2]}, {year})",
                        f"({authors[0]}, {authors[1]}&{authors[2]}, {year})",
                        f"({authors[0]},{authors[1]}&{authors[2]}, {year})",
                        # 逗号分隔变体
                        f"({authors[0]}, {authors[1]}, {authors[2]}, {year})",
                        f"({authors[0]},{authors[1]}, {authors[2]}, {year})",
                        f"({authors[0]}, {authors[1]},{authors[2]}, {year})",
                        f"({authors[0]},{authors[1]},{authors[2]}, {year})",
                        # 逗号+&格式变体（针对类似'王希鹏, 王京龙, & 王仲孝'的格式）
                        f"({authors[0]}, {authors[1]}, & {authors[2]}, {year})",
                        f"({authors[0]},{authors[1]},& {authors[2]}, {year})",
                        f"({authors[0]}, {authors[1]},& {authors[2]}, {year})",
                        f"({authors[0]},{authors[1]}, &{authors[2]}, {year})",
                        # 中文格式变体
                        f"({authors[0]}、{authors[1]}和{authors[2]}, {year})",
                        f"({authors[0]}、{authors[1]}和 {authors[2]}, {year})",
                        f"({authors[0]}、{authors[1]} 和{authors[2]}, {year})",
                        f"({authors[0]}、{authors[1]} 和 {authors[2]}, {year})",
                        # 等格式变体
                        f"({authors[0]} et al., {year})",
                        f"({authors[0]}et al., {year})",
                        f"({authors[0]} 等, {year})",
                        f"({authors[0]}等, {year})",
                        f"({authors[0]}等, {year})"
                    ]
                    citation_variants.extend(base_formats)
                    
                else:
                    # 四位及以上作者变体
                    base_formats: List[str] = [
                        # et al.格式变体
                        f"({authors[0]} et al., {year})",
                        f"({authors[0]}et al., {year})",
                        f"({authors[0]} et al.,{year})",
                        f"({authors[0]}et al.,{year})",
                        # 等格式变体
                        f"({authors[0]} 等, {year})",
                        f"({authors[0]}等, {year})",
                        f"({authors[0]}等, {year})",
                        f"({authors[0]} 等,{year})",
                        f"({authors[0]}等,{year})",
                        # 可能的多作者完整格式（虽然少见但AI可能生成）
                        f"({authors[0]}, {authors[1]} et al., {year})",
                        f"({authors[0]}, {authors[1]}, et al., {year})",
                    ]
                    citation_variants.extend(base_formats)
                
                # 为所有变体创建映射
                for variant in citation_variants:
                    # 原始格式映射
                    citation_to_key[variant] = standard_citation
                    # 标准化格式映射（处理空格和标点差异）
                    normalized_variant = normalize_citation_for_mapping(variant)
                    if normalized_variant != variant:
                        citation_to_key[normalized_variant] = standard_citation
                
                # 额外处理：作者名之间可能有空格变体
                if len(authors) >= 2:
                    # 为双作者添加无空格变体
                    no_space_variant = f"({authors[0]}&{authors[1]}, {year})"
                    citation_to_key[no_space_variant] = standard_citation

        # 从Word文档中提取所有引用
        full_text: str = "\n".join([p.text for p in doc.paragraphs])
        sentences: List[str] = re.split(r'(?<=[.。?？!！])\s+', full_text)

        # 辅助函数：标准化引用字符串
        def normalize_citation(citation: str) -> str:
            """标准化引用字符串，统一标点和空格，处理中文格式变体"""
            if not citation:
                return citation
            
            # 1. 移除多余空格，将多个空格合并为一个
            citation = re.sub(r'\s+', ' ', citation).strip()
            
            # 2. 统一标点：中文标点替换为英文标点
            citation = citation.replace('；', ';').replace('，', ',').replace('、', ',')
            
            # 3. 移除常见的中文前缀（如"支持文献:"、"参见:"、"来源:"等）
            citation = re.sub(r'\(支持文献[:：]\s*', '(', citation)
            citation = re.sub(r'\(参见[:：]\s*', '(', citation)
            citation = re.sub(r'\(来源[:：]\s*', '(', citation)
            citation = re.sub(r'\(引用自[:：]\s*', '(', citation)
            
            # 4. 统一"和"与"&" - 更安全的替换，避免替换作者名中的"和"
            # 只替换作为连接词的"和"（前面有作者名，后面有作者名或空格）
            # 先处理带空格的" 和 "，再处理紧邻的"和"
            citation = re.sub(r'([a-zA-Z\u4e00-\u9fff]+)\s+和\s+([a-zA-Z\u4e00-\u9fff]+)', r'\1 & \2', citation)
            citation = re.sub(r'([a-zA-Z\u4e00-\u9fff]+)和([a-zA-Z\u4e00-\u9fff]+)', r'\1 & \2', citation)
            
            # 5. 统一"等"与"et al." - 更全面的处理
            # 处理"等"后面跟逗号的情况，包括中文逗号（已统一为英文逗号）
            citation = re.sub(r'等\s*,', ' et al.,', citation)
            # 处理"等"后面跟分号的情况
            citation = re.sub(r'等\s*;', ' et al.;', citation)
            # 处理"等"后面跟右括号的情况
            citation = re.sub(r'等\s*\)', ' et al.)', citation)
            # 处理"等"前面有空格的情况
            citation = re.sub(r'\s等\s*,', ' et al.,', citation)
            # 处理单独的"等"（后面没有标点，理论上不应该出现）
            citation = re.sub(r'等\s+', ' et al. ', citation)
            
            # 6. 确保年份前有空格，同时处理年份后的空格
            citation = re.sub(r',(\d{4})', r', \1', citation)
            citation = re.sub(r',\s+(\d{4})', r', \1', citation)  # 多个空格变体
            citation = re.sub(r',\s*(\d{4})\s*\)', r', \1)', citation)  # 年份后空格
            
            # 7. 规范化作者分隔符和空格
            citation = re.sub(r'\(\s*', '(', citation)
            citation = re.sub(r'\s*\)', ')', citation)
            citation = re.sub(r'\s*,\s*', ', ', citation)
            citation = re.sub(r'\s*&\s*', ' & ', citation)
            
            # 7.5 处理逗号+&格式（如"作者1, & 作者2"）
            citation = re.sub(r',\s*&', ' &', citation)
            
            # 8. 清理可能产生的双逗号（如"et al.,,"）和多余空格
            citation = re.sub(r',\s*,', ', ', citation)
            citation = re.sub(r'et al\.\s*,', 'et al.,', citation)
            citation = re.sub(r'\s+', ' ', citation)  # 再次合并多余空格
            
            # 8.5 最终空格规范化
            citation = citation.replace('( ', '(').replace(' )', ')')
            citation = re.sub(r'\s+', ' ', citation)  # 最终空格合并
            
            return citation
        
        # 辅助函数：从句子中提取所有引用（正确处理多个引用）
        def extract_citations_from_sentence(sentence: str) -> List[str]:
            """从句子中提取所有引用，正确处理多个引用和中文标点"""
            citations: List[str] = []
            
            # 首先，匹配所有可能包含多个引用的模式
            # 模式：以括号开头，包含逗号和年份，可能由分号分隔多个引用
            # 例如：(作者1, 年份; 作者2, 年份) 或 (作者1, 年份) 等
            multi_citation_pattern = r'\([^)]+,\s*\d{4}(?:[;；]\s*[^)]+,\s*\d{4})*\)'
            multi_matches = re.findall(multi_citation_pattern, sentence)
            
            for match in multi_matches:
                # 移除外层括号
                inner = match[1:-1].strip()
                if not inner:
                    continue
                    
                # 按中文或英文分号分割
                parts = re.split(r'[；;]\s*', inner)
                for part in parts:
                    if not part.strip():
                        continue
                        
                    # 确保部分有括号
                    part_stripped = part.strip()
                    if not part_stripped.startswith('('):
                        part_stripped = '(' + part_stripped
                    if not part_stripped.endswith(')'):
                        part_stripped = part_stripped + ')'
                    
                    # 验证是否为有效的引用格式
                    if re.match(r'^\([^)]+,\s*\d{4}\)$', part_stripped):
                        citations.append(part_stripped)
            
            # 如果未找到多个引用模式，尝试直接匹配单个引用
            if not citations:
                single_matches = re.findall(r'\([^)]+,\s*\d{4}\)', sentence)
                citations.extend(single_matches)
            
            # 去重并返回
            return list(dict.fromkeys(citations))  # 保持顺序的去重

        all_found_citations: set[str] = set()
        citation_locations: Dict[str, List[str]] = {}  # {'(Author, YYYY)': [sentence1, sentence2, ...]}

        for sentence in sentences:
            citations_in_sentence: List[str] = extract_citations_from_sentence(sentence)
            for citation in citations_in_sentence:
                # 标准化引用
                normalized_citation = normalize_citation(citation)
                
                # 尝试查找映射：先尝试原始引用，再尝试标准化后的引用
                mapped_key: str = citation_to_key.get(citation, citation)
                if mapped_key == citation:  # 原始引用未找到映射
                    mapped_key = citation_to_key.get(normalized_citation, citation)
                
                all_found_citations.add(citation)
                if mapped_key not in citation_locations:
                    citation_locations[mapped_key] = []
                citation_locations[mapped_key].append(sentence.strip())

        # --- 2. 幻觉引用检查 ---
        phantom_citations: List[str] = sorted(list(all_found_citations - set(citation_to_key.keys()) - set(valid_citation_map.keys())))
        report_lines: List[str] = ["llm_reviewer_generator文献综述验证报告", f"生成时间: {datetime.now().isoformat()}\n", "="*30]
        if phantom_citations:
            generator_instance.logger.error(f"发现 {len(phantom_citations)} 处可能的幻觉引用！")
            report_lines.append("【幻觉引用检查 - 失败】\n以下引用未在您的文献库中找到：\n" + "\n".join(phantom_citations))
        else:
            generator_instance.logger.success("引用来源检查通过，未发现幻觉引用。")
            report_lines.append("【幻觉引用检查 - 通过】\n所有引用均来自提供的文献库。")

        # --- 3. 批量观点-引用匹配检查 ---
        generator_instance.logger.info("步骤2/3: 正在批量进行观点-引用匹配检查...")
        mismatch_reports: List[Dict[str, str]] = []
        if not api_config_valid:
            generator_instance.logger.error("Validator_API的api_key或model未在配置中找到。跳过观点匹配检查。")
        else:
            papers_to_validate: Dict[str, List[str]] = {key: sentences for key, sentences in citation_locations.items() if sentences and key in valid_citation_map}
            
            iterator = papers_to_validate.items()
            if TQDM_AVAILABLE:
                iterator = tqdm(iterator, desc="[验证] 逐篇文献批量核对")

            for paper_key, sentences_for_validation in iterator:
                source_summary: Dict[str, Any] = valid_citation_map[paper_key]
                title: str = source_summary.get('paper_info', {}).get('title', 'N/A')
                if TQDM_AVAILABLE:
                    iterator.set_postfix_str(f"核对: {title[:30]}...")  # type: ignore
                else:
                    generator_instance.logger.info(f"正在核对: {title[:30]}...")
                
                # 去重句子列表，减少不必要的API调用
                unique_sentences: List[str] = sorted(list(set(sentences_for_validation)))

                validation_result: Optional[Dict[str, Any]] = _validate_claims_for_single_paper(source_summary, unique_sentences, validator_api_config, generator_instance.config)  # type: ignore
                
                if validation_result:
                    for claim in validation_result.get('claims', []):
                        sentence: str = claim.get('sentence', '')
                        status: str = claim.get('status', 'UNKNOWN')
                        reason: str = claim.get('reason', '')
                        suggestion: str = claim.get('suggestion', '')
                        
                        if status in ['UNSUPPORTED', 'PARTIAL_SUPPORT']:
                            mismatch_reports.append({
                                'citation': paper_key,
                                'title': title,
                                'sentence': sentence,
                                'status': status,
                                'reason': reason,
                                'suggestion': suggestion
                            })

        # --- 4. 生成结构化报告 ---
        generator_instance.logger.info("步骤3/3: 正在生成验证报告...")
        if mismatch_reports:
            generator_instance.logger.error(f"发现 {len(mismatch_reports)} 处观点-引用不匹配！")
            report_lines.append("\n【观点-引用匹配检查 - 失败】\n以下论点可能未得到文献充分支持：\n")
            
            for i, report in enumerate(mismatch_reports, 1):
                report_lines.append(f"\n{i}. 引用: {report['citation']}")
                report_lines.append(f"   论文: {report['title']}")
                report_lines.append(f"   状态: {report['status']}")
                report_lines.append(f"   原句: {report['sentence']}")
                report_lines.append(f"   理由: {report['reason']}")
                if report['suggestion']:
                    report_lines.append(f"   建议: {report['suggestion']}")
        else:
            if api_config_valid:
                generator_instance.logger.success("观点-引用匹配检查通过，所有论点均得到文献支持。")
                report_lines.append("\n【观点-引用匹配检查 - 通过】\n所有论点均得到文献支持。")
            else:
                report_lines.append("\n【观点-引用匹配检查 - 跳过】\n由于API配置问题，跳过此项检查。")

        # 保存报告
        report_file: str = os.path.join(generator_instance.output_dir, f'{generator_instance.project_name}_validation_report.txt')
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(report_lines))
        
        generator_instance.logger.info(f"验证报告已保存到: {report_file}")
        return {"success": True, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
        
    except (configparser.NoSectionError, configparser.NoOptionError):
        generator_instance.logger.error("无法找到[Validator_API]或[Performance]中的验证配置，跳过验证。")
        return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
    except Exception as e:
        generator_instance.logger.error(f"验证综述时发生未知异常: {e}")
        traceback.print_exc()
        return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}


def run_week3_review_validation(
    review_draft: Dict[str, Any],
    citation_manifest: Dict[str, Any],
    paper_artifacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Week 3 compatibility entry point for review validation using Week 2 artifacts."""
    from validation.review_validator import ReviewValidator

    validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts)
    report = validator.validate()

    return {
        "week3_validation": True,
        "report": report,
    }


def run_week3_summary_recheck(
    paper_artifacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Week 3 compatibility entry point for summary recheck using paper artifacts."""
    from validation.summary_recheck import run_summary_rechecks

    reports = run_summary_rechecks(paper_artifacts)

    return {
        "week3_recheck": True,
        "reports": reports,
    }
