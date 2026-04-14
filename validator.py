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
        validator_api_config = _get_validator_api_config(generator_instance)
        if not validator_api_config:
            generator_instance.logger.error("未找到有效的[Validator_API]配置，跳过验证。")
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

def _get_validation_workspace(generator_instance: Any) -> Any:
    if hasattr(generator_instance, "job_workspace") and generator_instance.job_workspace:
        return generator_instance.job_workspace

    from services.job_workspace import JobWorkspace

    project_name = generator_instance.project_name or "unknown_project"
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    return JobWorkspace(generator_instance.output_dir, project_name, job_id)


def _load_validation_inputs(generator_instance: Any) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    review_draft_path = generator_instance._review_draft_v2_path()
    if not os.path.exists(review_draft_path):
        generator_instance.logger.error(f"Missing review_draft_v2 file: {review_draft_path}")
        return None, None, [], {}, {}

    citation_manifest_path = generator_instance._citation_manifest_path()
    if not os.path.exists(citation_manifest_path):
        generator_instance.logger.error(f"Missing citation_manifest file: {citation_manifest_path}")
        return None, None, [], {}, {}

    with open(review_draft_path, "r", encoding="utf-8") as handle:
        review_draft = json.load(handle)
    with open(citation_manifest_path, "r", encoding="utf-8") as handle:
        citation_manifest = json.load(handle)

    paper_artifacts: List[Dict[str, Any]] = []
    try:
        from services.artifact_registry import ArtifactRegistry

        workspace = _get_validation_workspace(generator_instance)
        registry = generator_instance.artifact_registry or ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
        for record in registry.list_records():
            if record.artifact_type == "paper_artifact" and record.status == "ready":
                try:
                    with open(record.path, "r", encoding="utf-8") as handle:
                        paper_artifacts.append(json.load(handle))
                except Exception as exc:
                    generator_instance.logger.warning(f"?? paper artifact ??: {exc}")
    except Exception as exc:
        generator_instance.logger.warning(
            f"Failed to load paper artifacts from the artifact registry; falling back to summaries: {exc}"
        )

    if not paper_artifacts:
        for summary in getattr(generator_instance, "summaries", []) or []:
            paper_info = summary.get("paper_info", {})
            ai_summary = summary.get("ai_summary", {})
            canonical_key = generator_instance.get_paper_key(paper_info)
            paper_artifacts.append(
                {
                    "paper_identity": {
                        "canonical_paper_key": canonical_key,
                        "source_paper_id": paper_info.get("pdf_path", ""),
                    },
                    "analysis": {"ai_summary": ai_summary},
                    "source": {"source_pdf": paper_info.get("pdf_path", "")},
                    "stage1_inputs": {},
                }
            )

    preprocess_evidence: Dict[str, Any] = {}
    paper_metadata: Dict[str, Any] = {}
    for artifact in paper_artifacts:
        paper_key = artifact.get("paper_identity", {}).get("canonical_paper_key", "")
        source_paper_id = artifact.get("paper_identity", {}).get("source_paper_id", "")
        stage1_inputs = artifact.get("stage1_inputs", {})
        identity = artifact.get("paper_identity", {})
        if paper_key:
            preprocess_evidence[paper_key] = stage1_inputs.get("preprocess_evidence", {})
            paper_metadata[paper_key] = identity
        if source_paper_id:
            preprocess_evidence[source_paper_id] = stage1_inputs.get("preprocess_evidence", {})
            paper_metadata[source_paper_id] = identity

    return review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata


def _build_report_from_results(citation_results: List[Any]) -> Any:
    from validation.review_validator import ReviewValidationReport, ValidationConclusion

    return ReviewValidationReport(
        report_id=f"validation_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        created_at=datetime.now().isoformat(),
        total_citations=len(citation_results),
        supported_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.SUPPORTED),
        partial_support_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.PARTIAL_SUPPORT),
        unsupported_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.UNSUPPORTED),
        wrong_source_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.WRONG_SOURCE),
        needs_review_count=sum(1 for item in citation_results if item.conclusion == ValidationConclusion.NEEDS_REVIEW),
        citation_results=citation_results,
    )


def _get_validator_api_config(generator_instance: Any) -> Optional[APIConfig]:
    validator_config: Dict[str, Any] = (generator_instance.config.get("Validator_API") or {}) if generator_instance.config else {}
    api_key = str(validator_config.get("api_key") or "").strip()
    model = str(validator_config.get("model") or "").strip()
    if not api_key or not model:
        return None
    return APIConfig(
        api_key=api_key,
        model=model,
        api_base=str(validator_config.get("api_base") or "https://api.openai.com/v1").strip(),
    )


def _load_source_text_for_artifact(paper_artifact: Dict[str, Any]) -> str:
    preprocess_evidence = paper_artifact.get("stage1_inputs", {}).get("preprocess_evidence", {})
    candidate_paths = [
        preprocess_evidence.get("normalized_md_path"),
        preprocess_evidence.get("normalized_path"),
        preprocess_evidence.get("plain_text_path"),
    ]
    for file_path in candidate_paths:
        if file_path and os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
                    content = handle.read().strip()
                if content:
                    return content
            except Exception:
                pass
    return str(paper_artifact.get("source", {}).get("source_text") or "")


def _map_ai_bundle_result(result: Any, ai_report: Dict[str, Any]) -> Any:
    from validation.review_validator import CitationValidationResult, RootCause, ValidationConclusion

    status = str(ai_report.get("status") or "").strip().lower()
    confidence = float(ai_report.get("confidence") or 0.0)
    repair_scope = str(ai_report.get("repair_scope") or "none").strip().lower()
    low_confidence = bool(ai_report.get("low_confidence")) or confidence < 0.55 or status == "low_confidence"

    if status == "supported":
        conclusion = ValidationConclusion.SUPPORTED
        root_causes: List[RootCause] = []
    elif status in {"partial_support", "partial"}:
        conclusion = ValidationConclusion.PARTIAL_SUPPORT
        root_causes = [RootCause.INSUFFICIENT_CONTEXT]
    elif status in {"wrong_source", "mapping_error"}:
        conclusion = ValidationConclusion.WRONG_SOURCE
        root_causes = [RootCause.CITATION_MAPPING_ERROR]
    elif low_confidence:
        conclusion = ValidationConclusion.NEEDS_REVIEW
        root_causes = [RootCause.LOW_CONFIDENCE]
    else:
        conclusion = ValidationConclusion.UNSUPPORTED
        root_causes = []

    if repair_scope == "summary":
        root_causes.append(RootCause.SUMMARY_DRIFT)
    elif repair_scope == "review":
        root_causes.append(RootCause.REVIEW_DRIFT)
    elif repair_scope == "both":
        root_causes.extend([RootCause.SUMMARY_DRIFT, RootCause.REVIEW_DRIFT, RootCause.COMPOUND_DRIFT])

    if not root_causes and conclusion == ValidationConclusion.UNSUPPORTED:
        root_causes = [RootCause.INSUFFICIENT_CONTEXT]

    details = dict(result.details)
    details.update(
        {
            "ai_validation": ai_report,
            "ai_confidence": confidence,
            "repair_scope": repair_scope,
            "summary_paper_ids": list(ai_report.get("summary_paper_ids") or result.paper_ids),
            "manual_review_reason": str(ai_report.get("manual_review_reason") or "").strip(),
        }
    )

    reasoning_summary = str(ai_report.get("reasoning") or result.reasoning_summary).strip() or result.reasoning_summary
    repair_hint = str(ai_report.get("repair_hint") or result.repair_hint).strip() or result.repair_hint

    return CitationValidationResult(
        citation_id=result.citation_id,
        paper_id=result.paper_id,
        conclusion=conclusion,
        root_causes=root_causes,
        evidence_candidates=result.evidence_candidates,
        details=details,
        claim_text=result.claim_text,
        claim_context=result.claim_context,
        evidence_excerpt_list=result.evidence_excerpt_list,
        reasoning_summary=reasoning_summary,
        repair_hint=repair_hint,
        citation_set_key=result.citation_set_key,
        paper_ids=result.paper_ids,
        block_ids=result.block_ids,
        low_confidence=low_confidence,
    )


def _run_ai_bundle_validation(generator_instance: Any, result: Any) -> Any:
    validator_api_config = _get_validator_api_config(generator_instance)
    if not validator_api_config or not result.claim_text.strip() or not result.paper_ids:
        return result

    try:
        max_tokens = int((generator_instance.config.get("API_Parameters") or {}).get("claims_max_tokens", 4096))
        temperature = float((generator_instance.config.get("API_Parameters") or {}).get("claims_temperature", 0.2))
    except Exception:
        max_tokens = 4096
        temperature = 0.2

    payload = {
        "citation_set_key": result.citation_set_key,
        "paper_ids": result.paper_ids,
        "claim_text": result.claim_text,
        "claim_context": result.claim_context,
        "evidence_excerpt_list": result.evidence_excerpt_list[:8],
    }
    prompt = (
        "You are validating a literature-review claim bundle against the exact cited paper set. "
        "Judge whether the claim is supported by the cited sources, whether the problem appears to come from "
        "the stage-1 summary, the review draft, both, or is too uncertain for automatic repair. "
        "Return JSON with keys: status, confidence, repair_scope, low_confidence, reasoning, repair_hint, "
        "summary_paper_ids, manual_review_reason.\n\n"
        f"Bundle payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    system_prompt = (
        "Return JSON only. status must be one of supported, partial_support, unsupported, "
        "wrong_source, low_confidence. repair_scope must be one of none, summary, review, both, manual_review."
    )

    try:
        ai_report = _call_ai_api(
            prompt,
            validator_api_config,
            system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format="json",
            logger=generator_instance.logger,
        )
    except Exception as exc:
        generator_instance.logger.warning(f"AI citation-set validation failed: {exc}")
        return result

    if not isinstance(ai_report, dict):
        return result
    return _map_ai_bundle_result(result, ai_report)


def _find_summary_entry_for_paper(generator_instance: Any, paper_id: str) -> Optional[Dict[str, Any]]:
    for summary in getattr(generator_instance, "summaries", []) or []:
        paper_info = summary.get("paper_info", {})
        if generator_instance.get_paper_key(paper_info) == paper_id:
            return summary
        if str(paper_info.get("pdf_path") or "").strip() == paper_id:
            return summary
    return None


def _apply_summary_repairs(generator_instance: Any, citation_results: List[Any], paper_artifacts: List[Dict[str, Any]]) -> List[str]:
    touched_papers: List[str] = []
    artifact_lookup = {
        artifact.get("paper_identity", {}).get("canonical_paper_key", ""): artifact
        for artifact in paper_artifacts
        if artifact.get("paper_identity", {}).get("canonical_paper_key")
    }
    for result in citation_results:
        repair_scope = str(result.details.get("repair_scope") or "").lower()
        if repair_scope not in {"summary", "both"} or result.low_confidence:
            continue
        for paper_id in list(dict.fromkeys(result.details.get("summary_paper_ids") or result.paper_ids)):
            summary_entry = _find_summary_entry_for_paper(generator_instance, paper_id)
            paper_artifact = artifact_lookup.get(paper_id)
            if summary_entry is None or paper_artifact is None:
                continue
            source_text = _load_source_text_for_artifact(paper_artifact)
            if not source_text:
                continue
            updated_summary = validate_paper_analysis(generator_instance, source_text, summary_entry.get("ai_summary", {}), use_cache=False)
            summary_entry["ai_summary"] = updated_summary
            paper_artifact.setdefault("analysis", {})["ai_summary"] = updated_summary
            touched_papers.append(paper_id)
            try:
                generator_instance._persist_paper_artifact(summary_entry)
            except Exception:
                pass

    if touched_papers:
        generator_instance.save_summaries()
    return list(dict.fromkeys(touched_papers))


def _rewrite_block_with_ai(
    generator_instance: Any,
    *,
    block_text: str,
    citation_tokens: List[str],
    claim_text: str,
    evidence_excerpt_list: List[str],
    paper_ids: List[str],
) -> Optional[str]:
    validator_api_config = _get_validator_api_config(generator_instance)
    if not validator_api_config:
        return None

    prompt = (
        "Rewrite the review block so that it remains academically toned, preserves the citation tokens exactly, "
        "and better matches the available source evidence. Return JSON with rewritten_block only.\n\n"
        f"Citation tokens: {json.dumps(citation_tokens, ensure_ascii=False)}\n"
        f"Paper ids: {json.dumps(paper_ids, ensure_ascii=False)}\n"
        f"Original block:\n{block_text}\n\n"
        f"Claim bundle summary:\n{claim_text}\n\n"
        f"Evidence excerpts:\n{json.dumps(evidence_excerpt_list[:8], ensure_ascii=False, indent=2)}"
    )
    system_prompt = "Return JSON only with rewritten_block. Preserve all citation tokens exactly."
    try:
        response = _call_ai_api(
            prompt,
            validator_api_config,
            system_prompt,
            max_tokens=4096,
            temperature=0.2,
            response_format="json",
            logger=generator_instance.logger,
        )
    except Exception as exc:
        generator_instance.logger.warning(f"AI review-block rewrite failed: {exc}")
        return None
    rewritten_block = str((response or {}).get("rewritten_block") or "").strip()
    if not rewritten_block:
        return None
    for token in citation_tokens:
        if token and token not in rewritten_block:
            rewritten_block = f"{rewritten_block} {token}".strip()
    return rewritten_block


def _apply_review_repairs(generator_instance: Any, review_draft: Dict[str, Any], citation_results: List[Any]) -> List[str]:
    touched_blocks: List[str] = []
    for result in citation_results:
        repair_scope = str(result.details.get("repair_scope") or "").lower()
        if repair_scope not in {"review", "both"} or result.low_confidence:
            continue
        citation_tokens = list(result.details.get("bundle", {}).get("citation_tokens") or [])
        for block_id in result.block_ids:
            for section in review_draft.get("content", {}).get("sections", []):
                for block in section.get("blocks", []):
                    if block.get("block_id") != block_id:
                        continue
                    rewritten = _rewrite_block_with_ai(
                        generator_instance,
                        block_text=str(block.get("text") or ""),
                        citation_tokens=citation_tokens,
                        claim_text=result.claim_text,
                        evidence_excerpt_list=result.evidence_excerpt_list,
                        paper_ids=result.paper_ids,
                    )
                    if rewritten and rewritten != block.get("text"):
                        block["text"] = rewritten
                        touched_blocks.append(block_id)
    return list(dict.fromkeys(touched_blocks))


def _rebuild_review_docx(generator_instance: Any, review_draft: Dict[str, Any], citation_manifest: Dict[str, Any], output_path: str) -> None:
    from docx_writer import append_section_to_word_document, generate_apa_references_from_manifest

    if os.path.exists(output_path):
        os.remove(output_path)

    for section in review_draft.get("content", {}).get("sections", []):
        section_text = "\n\n".join(
            str(block.get("text") or "").strip()
            for block in section.get("blocks", [])
            if str(block.get("text") or "").strip()
        )
        append_section_to_word_document(
            generator_instance,
            int(section.get("section_number") or 0),
            str(section.get("section_title") or ""),
            section_text,
            output_path,
        )

    if DOCX_AVAILABLE and Document is not None:
        doc = Document(output_path)
        doc.add_heading("References", level=1)
        for reference in generate_apa_references_from_manifest(citation_manifest, generator_instance):
            doc.add_paragraph(reference)
        doc.save(output_path)


def _persist_repaired_review_artifacts(generator_instance: Any, review_draft: Dict[str, Any]) -> Dict[str, Any]:
    from services.job_workspace import atomic_write_json

    review_draft_path = generator_instance._review_draft_v2_path()
    word_path = generator_instance._get_review_word_file_path()
    atomic_write_json(review_draft_path, review_draft)
    generator_instance._persist_citation_manifest(
        review_draft_path=review_draft_path,
        review_word_path=word_path,
        citations=[],
    )
    with open(generator_instance._citation_manifest_path(), "r", encoding="utf-8") as handle:
        citation_manifest = json.load(handle)
    _rebuild_review_docx(generator_instance, review_draft, citation_manifest, word_path)
    return citation_manifest


def _write_validation_reports(generator_instance: Any, report: Any, manual_review_items: List[Any]) -> Dict[str, str]:
    workspace = _get_validation_workspace(generator_instance)
    project_name = workspace.project_name
    os.makedirs(workspace.paths.reports_dir, exist_ok=True)
    report_file = os.path.join(workspace.paths.reports_dir, f"{project_name}_validation_report.txt")
    manual_report_file = os.path.join(workspace.paths.reports_dir, f"{project_name}_manual_review_report.json")

    lines = ["auto-generate validation report", f"generated_at: {datetime.now().isoformat()}", "=" * 40]
    lines.append("summary")
    lines.append(f"total_citation_sets: {report.total_citations}")
    lines.append(f"supported: {report.supported_count}")
    lines.append(f"partial_support: {report.partial_support_count}")
    lines.append(f"unsupported: {report.unsupported_count}")
    lines.append(f"wrong_source: {report.wrong_source_count}")
    lines.append(f"needs_review: {report.needs_review_count}")
    lines.append("")
    lines.append("details")
    for index, result in enumerate(report.citation_results, start=1):
        lines.append(f"{index}. citation_set: {result.citation_set_key or result.citation_id}")
        lines.append(f"   papers: {', '.join(result.paper_ids) if result.paper_ids else result.paper_id}")
        lines.append(f"   conclusion: {result.conclusion.value}")
        lines.append(f"   root_causes: {', '.join(root.value for root in result.root_causes) or '?'}")
        lines.append(f"   claim: {result.claim_text[:300]}")
        lines.append(f"   reasoning: {result.reasoning_summary}")
        if result.repair_hint:
            lines.append(f"   repair_hint: {result.repair_hint}")
        lines.append("")

    with open(report_file, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    manual_payload = {
        "generated_at": datetime.now().isoformat(),
        "total_items": len(manual_review_items),
        "items": [
            {
                "citation_set_key": item.citation_set_key,
                "paper_ids": item.paper_ids,
                "claim_text": item.claim_text,
                "reasoning_summary": item.reasoning_summary,
                "repair_hint": item.repair_hint,
                "manual_review_reason": item.details.get("manual_review_reason", ""),
            }
            for item in manual_review_items
        ],
    }
    with open(manual_report_file, "w", encoding="utf-8") as handle:
        json.dump(manual_payload, handle, ensure_ascii=False, indent=2)

    return {"report_file": report_file, "manual_report_file": manual_report_file}


def run_review_validation(generator_instance: Any) -> dict:  # type: ignore
    generator_instance.logger.info("=" * 60 + "\nStarting review validation\n" + "=" * 60)
    try:
        if not generator_instance.config.getboolean("Performance", "enable_stage2_validation", fallback=False):  # type: ignore
            generator_instance.logger.warning("Stage-2 validation is disabled; skipping review validation.")  # type: ignore
            return {"success": True, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}

        review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata = _load_validation_inputs(generator_instance)
        if review_draft is None or citation_manifest is None:
            return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}

        from validation.review_validator import ReviewValidator

        validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata)
        base_report = validator.validate()
        enriched_results = [_run_ai_bundle_validation(generator_instance, result) for result in base_report.citation_results]
        final_report = _build_report_from_results(enriched_results)

        touched_summaries = _apply_summary_repairs(generator_instance, enriched_results, paper_artifacts)
        touched_blocks = _apply_review_repairs(generator_instance, review_draft, enriched_results)
        if touched_blocks:
            citation_manifest = _persist_repaired_review_artifacts(generator_instance, review_draft)

        if touched_summaries or touched_blocks:
            generator_instance.logger.info("Repairs applied; re-running review validation.")
            review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata = _load_validation_inputs(generator_instance)
            if review_draft is None or citation_manifest is None:
                return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
            validator = ReviewValidator(review_draft, citation_manifest, paper_artifacts, preprocess_evidence, paper_metadata)
            revalidated = validator.validate()
            final_report = _build_report_from_results([_run_ai_bundle_validation(generator_instance, result) for result in revalidated.citation_results])

        manual_review_items = [
            result
            for result in final_report.citation_results
            if result.low_confidence or result.conclusion.value == "NEEDS_REVIEW" or str(result.details.get("repair_scope") or "").lower() == "manual_review"
        ]
        report_paths = _write_validation_reports(generator_instance, final_report, manual_review_items)
        generator_instance.logger.success(f"Validation report written: {report_paths['report_file']}")
        generator_instance.logger.info(f"Manual review report written: {report_paths['manual_report_file']}")

        return {
            "success": True,
            "report": final_report,
            "review_draft": review_draft,
            "citation_manifest": citation_manifest,
            "paper_artifacts": paper_artifacts,
            "manual_review_items": manual_review_items,
            **report_paths,
        }

    except (configparser.NoSectionError, configparser.NoOptionError):
        generator_instance.logger.error("Validation configuration is incomplete.")
        return {"success": False, "report": None, "review_draft": None, "citation_manifest": None, "paper_artifacts": None}
    except Exception as exc:
        generator_instance.logger.error(f"Review validation failed: {exc}")
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
