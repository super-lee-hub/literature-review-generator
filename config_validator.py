#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置验证模块
统一处理所有配置相关的验证逻辑，遵循DRY原则
"""

import os
import re
import requests  # type: ignore
import json
from typing import Tuple, Dict, Any, List

from services.model_capabilities import resolve_model_capability
from services.repair_policy import parse_repair_policy
from services.proxy_policy import should_bypass_environment_proxy


class ConfigValidationError(Exception):
    """配置验证异常类"""
    pass


def _normalize_config_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_api_transport_combo(section_name: str, section: Dict[str, Any]) -> List[str]:
    """Validate provider/endpoint/reasoning fields that shape request payloads."""
    warnings: List[str] = []
    provider_family = _normalize_config_text(section.get("provider_family")).replace("-", "_").lower()
    endpoint_type = _normalize_config_text(section.get("endpoint_type")).replace("-", "_").lower()
    api_base = _normalize_config_text(section.get("api_base")).lower()
    reasoning_effort = _normalize_config_text(section.get("reasoning_effort"))
    reasoning_display = _normalize_config_text(section.get("reasoning_display"))
    thinking = _normalize_config_text(section.get("thinking"))

    if provider_family and provider_family not in {"aihubmix_openai", "aihubmix_claude", "deepseek", "generic"}:
        warnings.append(f"[{section_name}] provider_family={provider_family!r} is not supported")
    if endpoint_type and endpoint_type not in {"chat_completions", "responses", "response"}:
        warnings.append(f"[{section_name}] endpoint_type={endpoint_type!r} is not supported")
    if provider_family == "aihubmix_openai" and endpoint_type and endpoint_type not in {"responses", "response"}:
        warnings.append(f"[{section_name}] aihubmix_openai reasoning config requires endpoint_type=responses")
    if provider_family == "aihubmix_claude" and endpoint_type and endpoint_type not in {"chat_completions"}:
        warnings.append(f"[{section_name}] aihubmix_claude requires endpoint_type=chat_completions")
    if provider_family == "deepseek" and endpoint_type and endpoint_type not in {"chat_completions"}:
        warnings.append(f"[{section_name}] deepseek requires endpoint_type=chat_completions")
    if provider_family == "deepseek" and api_base and "deepseek" not in api_base:
        warnings.append(f"[{section_name}] provider_family=deepseek should use a DeepSeek api_base")
    if provider_family.startswith("aihubmix") and api_base and "aihubmix" not in api_base:
        warnings.append(f"[{section_name}] provider_family={provider_family} should use an AIHubMix api_base")

    capability = resolve_model_capability(section)  # type: ignore[arg-type]
    if (reasoning_effort or reasoning_display or thinking) and not capability.supports_reasoning:
        warnings.append(f"[{section_name}] reasoning fields are set but provider/model combination does not support reasoning")
    if reasoning_display and capability.reasoning_param_style != "chat_reasoning":
        warnings.append(f"[{section_name}] reasoning_display is only valid for chat reasoning providers")
    if thinking and capability.reasoning_param_style != "deepseek_thinking":
        warnings.append(f"[{section_name}] thinking is only valid for DeepSeek reasoning")
    return warnings


def validate_file_path(path: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证文件路径
    
    Args:
        path: 文件路径
        allow_empty: 是否允许空路径（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not path:
        if allow_empty:
            return True, ""
        return False, "文件路径不能为空"
    
    # 安全检查：防止路径遍历攻击
    if ".." in path:
        return False, "路径不能包含'..'"
    
    # 检查路径是否存在
    if not os.path.isfile(path):
        return False, "文件不存在"
    
    return True, ""


def validate_directory_path(path: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证目录路径
    
    Args:
        path: 目录路径
        allow_empty: 是否允许空路径（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not path:
        if allow_empty:
            return True, ""
        return False, "目录路径不能为空"
    
    # 安全检查：防止路径遍历攻击
    if ".." in path:
        return False, "路径不能包含'..'"
    
    # 检查路径是否存在
    if not os.path.isdir(path):
        return False, "目录不存在"
    
    return True, ""


def validate_output_path(path: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证输出目录路径（可以是不存在的目录，但需要能创建）
    
    Args:
        path: 输出目录路径
        allow_empty: 是否允许空路径（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not path:
        if allow_empty:
            return True, ""
        return False, "输出路径不能为空"
    
    # 安全检查：防止路径遍历攻击
    if ".." in path:
        return False, "路径不能包含'..'"
    
    # 尝试创建目录（如果不存在）
    try:
        os.makedirs(path, exist_ok=True)
        # 检查是否可写
        test_file = os.path.join(path, '.permission_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        return True, ""
    except (OSError, IOError) as e:
        return False, f"无法创建或写入目录: {e}"


def validate_api_key(api_key: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证API密钥格式
    
    Args:
        api_key: API密钥
        allow_empty: 是否允许空值
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not api_key:
        if allow_empty:
            return True, ""
        return False, "API Key不能为空"
    
    # 去除首尾空格
    api_key = api_key.strip()
    
    # 检查前缀
    if not api_key.startswith("sk-"):
        return False, "API Key应该以'sk-'开头"
    
    # 检查长度（API Key通常有特定长度要求）
    if len(api_key) < 20:
        return False, "API Key长度似乎过短，请确认是否正确"
    
    return True, ""


def validate_url(url: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证URL格式
    
    Args:
        url: URL字符串
        allow_empty: 是否允许空值（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not url:
        if allow_empty:
            return True, ""
        return False, "URL不能为空"
    
    # 去除首尾空格和末尾的斜杠
    url = url.strip().rstrip('/')
    
    # 检查协议前缀
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "URL应该以'http://'或'https://'开头"
    
    # 简单的URL格式验证
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
        r'localhost|'  # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    
    if not url_pattern.match(url):
        return False, "URL格式不正确"
    
    return True, ""


def validate_numeric_range(value: str, min_val: int, max_val: int, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证数值范围
    
    Args:
        value: 字符串形式的数值
        min_val: 最小值
        max_val: 最大值
        allow_empty: 是否允许空值（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not value:
        if allow_empty:
            return True, ""
        return False, "值不能为空"
    
    try:
        int_value = int(value)
        if int_value < min_val or int_value > max_val:
            return False, f"值应在{min_val}-{max_val}之间"
        return True, ""
    except ValueError:
        return False, "请输入一个有效的数字"


def validate_positive_number(value: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证正数
    
    Args:
        value: 字符串形式的数值
        allow_empty: 是否允许空值（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not value:
        if allow_empty:
            return True, ""
        return False, "值不能为空"
    
    try:
        int_value = int(value)
        if int_value <= 0:
            return False, "值应大于0"
        return True, ""
    except ValueError:
        return False, "请输入一个有效的数字"


def validate_positive_number_or_zero(value: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证正数或零（用于适应性速率控制）
    
    Args:
        value: 字符串形式的数值
        allow_empty: 是否允许空值（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not value:
        if allow_empty:
            return True, ""
        return False, "值不能为空"
    
    try:
        int_value = int(value)
        if int_value < 0:
            return False, "值应大于或等于0"
        return True, ""
    except ValueError:
        return False, "请输入一个有效的数字"


def validate_model_name(model: str, allow_empty: bool = False) -> Tuple[bool, str]:
    """
    验证模型名称
    
    Args:
        model: 模型名称
        allow_empty: 是否允许空值（使用默认值）
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if not model:
        if allow_empty:
            return True, ""
        return False, "模型名称不能为空"
    
    # 去除首尾空格
    model = model.strip()
    
    if not model:
        return False, "模型名称不能为空"
    
    # 模型名称通常包含字母、数字、点、斜杠和连字符
    if not re.match(r'^[a-zA-Z0-9./-]+$', model):
        return False, "模型名称包含无效字符"
    
    return True, ""


def validate_config_section(config_dict: Dict[str, Any], section_name: str, required_keys: List[str]) -> Tuple[bool, str]:
    """
    验证配置段
    
    Args:
        config_dict: 配置字典
        section_name: 段名称
        required_keys: 必需的键列表
        
    Returns:
        Tuple[bool, str]: (是否有效, 错误信息)
    """
    if section_name not in config_dict:
        return False, f"缺少配置段: [{section_name}]"
    
    section = config_dict[section_name]
    for key in required_keys:
        if key not in section or not section[key].strip():
            return False, f"配置项[{section_name}]{key}不能为空"
    
    return True, ""


def validate_all_config(config_dict: Dict[str, Any]) -> Tuple[bool, List[str]]:


    """


    验证所有配置


    


    Args:


        config_dict: 配置字典


        


    Returns:


        Tuple[bool, list]: (是否有效, 警告信息列表)


    """


    warnings: List[str] = []


    


    # 验证路径配置：output_path 必填，Zotero 路径允许在 PDF 文件夹模式下留空。
    if 'Paths' not in config_dict:
        return False, ["缺少配置段: [Paths]"]
    if not str(config_dict['Paths'].get('output_path', '')).strip():
        return False, ["配置项[Paths]output_path不能为空"]


    


    # 验证API配置


    api_sections = ['Primary_Reader_API', 'Backup_Reader_API', 'Writer_API']


    for section in api_sections:


        valid, error = validate_config_section(config_dict, section, ['api_key', 'model', 'api_base'])


        if not valid:


            return False, [error]


        


        # 验证API Key格式


        api_key: str = config_dict[section]['api_key']


        valid, error = validate_api_key(api_key)


        if not valid:


            warnings.append(f"[{section}] {error}")

        warnings.extend(_validate_api_transport_combo(section, config_dict[section]))


        


        # 验证URL格式


        api_base: str = config_dict[section]['api_base']


        valid, error = validate_url(api_base)


        if not valid:


            warnings.append(f"[{section}] {error}")


    


    # 验证性能配置（可选）


    if 'Performance' in config_dict:


        perf_config: Any = config_dict['Performance']


        


        # 验证max_workers


        if 'max_workers' in perf_config:


            valid, error = validate_numeric_range(perf_config['max_workers'], 1, 10)


            if not valid:


                warnings.append(f"[Performance] max_workers {error}")


        


        # 验证api_retry_attempts


        if 'api_retry_attempts' in perf_config:


            valid, error = validate_numeric_range(perf_config['api_retry_attempts'], 1, 10)


            if not valid:


                warnings.append(f"[Performance] api_retry_attempts {error}")


        


        # 验证速率限制（允许0值，用于适应性速率控制）


        rate_limit_keys = ['primary_tpm_limit', 'primary_rpm_limit', 'backup_tpm_limit', 'backup_rpm_limit']


        for key in rate_limit_keys:


            if key in perf_config:


                valid, error = validate_positive_number_or_zero(perf_config[key])


                if not valid:


                    warnings.append(f"[Performance] {key} {error}")

    if 'Outline_API' in config_dict and any(str(v).strip() for v in config_dict['Outline_API'].values()):
        valid, error = validate_config_section(config_dict, 'Outline_API', ['api_key', 'model', 'api_base'])
        if not valid:
            return False, [error]
        outline_api_base: str = config_dict['Outline_API']['api_base']
        valid, error = validate_url(outline_api_base)
        if not valid:
            warnings.append(f"[Outline_API] {error}")
        warnings.extend(_validate_api_transport_combo('Outline_API', config_dict['Outline_API']))

    if 'Free_Mode_API' in config_dict and any(str(v).strip() for v in config_dict['Free_Mode_API'].values()):
        valid, error = validate_config_section(config_dict, 'Free_Mode_API', ['api_key', 'model', 'api_base'])
        if not valid:
            return False, [error]
        free_mode_api_base: str = config_dict['Free_Mode_API']['api_base']
        valid, error = validate_url(free_mode_api_base)
        if not valid:
            warnings.append(f"[Free_Mode_API] {error}")
        warnings.extend(_validate_api_transport_combo('Free_Mode_API', config_dict['Free_Mode_API']))

    if 'Preprocess' in config_dict:
        ocr_mode = str(config_dict['Preprocess'].get('ocr_mode', 'auto')).lower()
        if ocr_mode not in {'auto', 'off', 'always'}:
            warnings.append("[Preprocess] ocr_mode 应为 auto/off/always 之一")


    


    if 'Validation' in config_dict:
        try:
            parse_repair_policy(config_dict['Validation'].get('repair_policy'))
        except ValueError as exc:
            return False, [str(exc)]

    return True, warnings





def test_api_connection(api_key: str, api_base: str, model: str, proxy_mode: str = "environment") -> Tuple[bool, str]:


    """


    测试API连通性


    


    Args:


        api_key: API密钥


        api_base: API基础URL


        model: 模型名称


        


    Returns:


        Tuple[bool, str]: (是否连通, 详细信息)


    """


    # 规范化URL


    api_base = api_base.rstrip('/')
    api_base = re.sub(r'/chat/completions/?$', '', api_base, flags=re.IGNORECASE)
    api_base = re.sub(r'/v1/chat/completions/?$', '/v1', api_base, flags=re.IGNORECASE)
    api_base = re.sub(r'/models/?$', '', api_base, flags=re.IGNORECASE)
    if not api_base.endswith('/v1'):
        api_base = f"{api_base}/v1"

    models_endpoint = f"{api_base}/models"


    


    headers = {


        "Authorization": f"Bearer {api_key}",


        "Content-Type": "application/json"


    }


    


    try:


        # 发送请求到模型列表端点


        if should_bypass_environment_proxy({"proxy_mode": proxy_mode}):
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    models_endpoint,
                    headers=headers,
                    timeout=10,  # 10秒超时
                )
        else:
            response = requests.get(
                models_endpoint,
                headers=headers,
                timeout=10,  # 10秒超时
            )


        


        # 检查HTTP状态码


        if response.status_code == 200:


            try:


                # 解析响应JSON


                data = response.json()


                models = data.get("data", [])


                model_ids = [m.get("id", "") for m in models]


                


                # 检查指定模型是否可用


                if model in model_ids:


                    return True, f"API连通成功，模型'{model}'可用"


                else:


                    # 尝试模糊匹配（部分API提供商会返回简化的模型名）


                    for model_id in model_ids:


                        if model.lower() in model_id.lower() or model_id.lower() in model.lower():


                            return True, f"API连通成功，找到匹配模型'{model_id}'"


                    


                    return False, f"模型不可用：API Key有效，但指定的模型'{model}'在该平台上不存在或无权访问。可用模型: {', '.join(model_ids[:5])}{'...' if len(model_ids) > 5 else ''}"


            


            except (json.JSONDecodeError, KeyError):


                return False, "API响应格式异常：无法解析模型列表"


        


        elif response.status_code == 401:


            return False, "认证失败：API Key无效或权限不足"


        


        elif response.status_code == 403:


            return False, "权限不足：API Key无权访问模型列表"


        


        elif response.status_code == 404:


            return False, "端点不存在：API Base URL或有不正确，或该服务不支持模型列表查询"


        


        elif response.status_code == 429:


            return False, "请求频率限制：API调用过于频繁，请稍后再试"


        


        else:


            return False, f"API请求失败：HTTP {response.status_code} - {response.text[:100]}"


    


    except requests.exceptions.ConnectionError:


        return False, "连接失败：请检查API Base URL是否正确，以及网络连接是否通畅"


    


    except requests.exceptions.Timeout:


        return False, "连接超时：API服务器响应时间过长，请检查网络连接或稍后再试"


    


    except requests.exceptions.RequestException as e:


        return False, f"请求异常：{str(e)}"


    


    except Exception as e:


        return False, f"未知错误：{str(e)}"





def validate_zotero_library_path(library_path: str) -> Tuple[bool, str]:


    """


    验证Zotero库路径的完整性


    


    Args:


        library_path: Zotero库路径


        


    Returns:


        Tuple[bool, str]: (是否有效, 详细信息)


    """


    if not library_path:


        return False, "Zotero库路径不能为空"


    


    # 安全检查：防止路径遍历攻击


    if ".." in library_path:


        return False, "路径不能包含'..'"


    


    # 检查路径是否存在


    if not os.path.isdir(library_path):


        return False, "目录不存在"


    


    # 检查上级目录是否存在zotero.sqlite文件


    parent_dir = os.path.dirname(library_path)


    zotero_sqlite_path = os.path.join(parent_dir, "zotero.sqlite")


    


    if os.path.exists(zotero_sqlite_path):


        return True, "有效的Zotero存储库路径"


    else:


        # 检查当前目录是否有zotero.sqlite（有些用户可能直接指向Zotero主目录）


        current_zotero_sqlite = os.path.join(library_path, "zotero.sqlite")


        if os.path.exists(current_zotero_sqlite):


            return True, "有效的Zotero主目录路径"


        


        return False, f"警告：在路径'{library_path}'及其上级目录中均未找到'zotero.sqlite'文件。或非有效的Zotero存储库路径"

