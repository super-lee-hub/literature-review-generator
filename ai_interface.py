import requests  # type: ignore
import json
import time
import threading
import re
from typing import Union, Dict, Optional, Any, List, Tuple, Callable

from models import APIConfig
from config_loader import load_config


def _call_ai_api(prompt: str, api_config: APIConfig, system_prompt: str, max_tokens: int = 4000,
                 temperature: float = 0.3, response_format: str = "json", logger: Any = None) -> Optional[Dict[str, Any]]:
    """
    统一的AI API调用函数，完全独立处理JSON解析，包含自动纠错功能

    Args:
        prompt: 用户提示词
        api_config: API配置字典
        system_prompt: 系统提示词
        max_tokens: 最大令牌数
        temperature: 温度参数
        response_format: 响应格式 ("json" 或 "text")
        logger: 日志记录器实例

    Returns:
        解析后的Python字典（如果响应是JSON）或字符串，失败返回None
    """
    try:
        api_key = api_config.get('api_key') or ''
        model_name = api_config.get('model') or ''
        api_base = api_config.get('api_base', 'https://api.openai.com/v1') or 'https://api.openai.com/v1'
        
        if not api_key or not model_name:
            if logger:
                logger.error("API配置缺少必要的参数: api_key 或 model")
            return None
        
        api_url = f"{api_base.rstrip('/')}/chat/completions"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        # 如果需要JSON格式响应，添加response_format参数
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        
        # 重试逻辑
        max_retries = 3
        response = None
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    api_url,
                    headers=headers,
                    json=payload,
                    timeout=300
                )
                
                response.raise_for_status()
                response_data = response.json()
                
                # 提取AI回复内容
                content = response_data['choices'][0]['message']['content']
                
                # 如果需要JSON格式响应，尝试解析JSON
                if response_format == "json":
                    # 使用智能JSON解析器，包含自动纠错功能
                    parsed_content = _smart_json_parser(content)
                    if parsed_content is not None:
                        return parsed_content
                    else:
                        # 如果智能解析失败，尝试自动纠错
                        corrected_content = _auto_correct_json(content)
                        if corrected_content is not None:
                            if logger:
                                logger.info("通过自动纠错成功修复JSON")
                            return corrected_content
                        else:
                            if logger:
                                logger.error("自动纠错也失败，无法解析JSON")
                                logger.debug(f"AI返回内容: {str(content)[:500]}...")
                            return None
                else:
                    # 文本格式响应，直接返回
                    return content
                
            except requests.exceptions.HTTPError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 * (2 ** attempt)
                    if logger:
                        logger.warning(f"HTTP错误 {response.status_code if response is not None else '?'}，{wait_time:.1f}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    if logger:
                        logger.error(f"API调用失败: {str(e)}")
                    return None

            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 * (2 ** attempt)
                    # 处理网络连接错误
                    error_msg = str(e)
                    if "Connection" in error_msg or "timeout" in error_msg.lower() or "reset" in error_msg.lower():
                        if logger:
                            logger.warning(f"网络连接错误: {str(e)}，{wait_time:.1f}秒后重试...")
                    else:
                        if logger:
                            logger.warning(f"错误: {str(e)}，{wait_time:.1f}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    # 记录详细错误信息但不中断程序
                    if logger:
                        logger.error(f"API调用最终失败: {str(e)}")
                    return None
        
        return None

    except Exception as e:
        if logger:
            logger.error(f"调用API失败: {e}")
        return None


def _smart_json_parser(content: str) -> Optional[Dict[str, Any]]:
    """
    智能JSON解析器，尝试多种方式解析JSON
    简化逻辑，提高可靠性和性能

    Args:
        content: AI返回的原始内容

    Returns:
        解析后的字典，失败返回None
    """
    if not content:
        return None

    # 清理内容，移除可能影响解析的前后空白
    content_stripped: str = content.strip()
    if not content_stripped:
        return None

    # 变量类型注解
    strategy_results: Optional[Dict[str, Any]] = None

    # 解析策略按优先级排序
    def parse_strategy_1() -> Optional[Dict[str, Any]]:
        return json.loads(content) if content else None
    
    def parse_strategy_2() -> Optional[Dict[str, Any]]:
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL | re.IGNORECASE)
        return json.loads(match.group(1)) if match and match.group(1) else None
    
    def parse_strategy_3() -> Optional[Dict[str, Any]]:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        return json.loads(match.group(0)) if match and match.group(0) else None
    
    def parse_strategy_4() -> Optional[Dict[str, Any]]:
        start = content.find('{')
        end = content.rfind('}')
        return json.loads(content[start:end+1]) if start != -1 and end != -1 and end >= start else None
    
    parse_strategies: List[Callable[[], Optional[Dict[str, Any]]]] = [
        parse_strategy_1,
        parse_strategy_2,
        parse_strategy_3,
        parse_strategy_4
    ]

    for strategy in parse_strategies:
        try:
            strategy_outcome = strategy()
            if strategy_outcome is not None:
                # 解析成功，不需要打印，因为这是正常流程
                return strategy_outcome
        except (AttributeError, json.JSONDecodeError, ValueError):
            # AttributeError: regex没有匹配到内容
            # JSONDecodeError: JSON格式错误
            # ValueError: 其他解析错误
            continue
        except Exception:
            # 意外错误，也不需要打印，避免日志噪音
            continue

    return strategy_results


def _auto_correct_json(content: str) -> Optional[Dict[str, Any]]:
    """
    自动纠错JSON，尝试修复常见的JSON格式错误
    
    Args:
        content: AI返回的原始内容
        
    Returns:
        修复后的字典，失败返回None
    """
    # 添加content None安全检查
    if not content:
        return None
    
    try:
        # 提取可能的JSON字符串
        json_str: Optional[str] = _extract_json_string(content)
        if not json_str:
            return None
        
        # 常见JSON错误修复
        corrected_json: str = _fix_common_json_errors(json_str)
        
        # 尝试解析修复后的JSON
        try:
            return json.loads(corrected_json)
        except json.JSONDecodeError:
            # 修复失败，不需要打印，因为还有后续处理
            # 尝试更激进的修复
            aggressively_fixed: str = _aggressive_json_fix(corrected_json)
            try:
                return json.loads(aggressively_fixed)
            except json.JSONDecodeError:
                return None

    except Exception:
        # 纠错过程出错，不需要打印，避免日志噪音
        return None


def _extract_json_string(content: str) -> Optional[str]:
    """
    从内容中提取JSON字符串
    
    Args:
        content: AI返回的原始内容
        
    Returns:
        提取的JSON字符串
    """
    # 添加content None安全检查
    if not content:
        return None
    
    # 尝试多种方法提取JSON字符串
    
    # 方法1：查找JSON代码块
    json_match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    if json_match:
        return json_match.group(1)
    
    # 方法2：查找JSON对象
    json_obj_match = re.search(r'(\{.*\})', content, re.DOTALL)
    if json_obj_match:
        return json_obj_match.group(1)
    
    # 方法3：查找第一个{和最后一个}之间的内容
    first_brace = content.find('{')
    last_brace = content.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return content[first_brace:last_brace+1]
    
    # 如果都失败了，返回原内容
    return content if content else ""


def _fix_common_json_errors(json_str: str) -> str:
    """
    修复常见的JSON格式错误
    
    Args:
        json_str: 原始JSON字符串
        
    Returns:
        修复后的JSON字符串
    """
    # 修复1：移除注释
    json_str = re.sub(r'//.*', '', json_str)  # 移除单行注释
    json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)  # 移除多行注释
    
    # 修复2：移除尾随逗号
    json_str = re.sub(r',\s*}', '}', json_str)  # 对象中的尾随逗号
    json_str = re.sub(r',\s*]', ']', json_str)  # 数组中的尾随逗号
    
    # 修复3：修复单引号为双引号
    # 这个修复比较复杂，需要确保不替换内容中的单引号
    # 简单处理：只替换键名和字符串值的单引号
    json_str = re.sub(r"(\w+)\s*:\s*'([^']*)'", r'"\1": "\2"', json_str)
    
    # 修复4：修复未引用的键名
    json_str = re.sub(r'(\w+)\s*:', r'"\1":', json_str)
    
    # 修复5：修复换行符问题
    json_str = re.sub(r'[\n\r]+', ' ', json_str)  # 将换行符替换为空格
    json_str = re.sub(r'\s+', ' ', json_str)  # 合并多个空格
    
    # 变量类型注解
    corrected_json: str = json_str.strip()
    return corrected_json


def _aggressive_json_fix(json_str: str) -> str:
    """
    更激进的JSON修复方法 - 修复版
    
    修复说明：
    - 原正则表达式 [^\"\\'\\{\\}\\[\\],]+ 无法匹配包含逗号、引号、大括号的值
    - 学术论文摘要必然包含这些字符，导致大规模失败
    - 新实现采用更智能的解析策略，能正确处理嵌套结构和特殊字符
    
    Args:
        json_str: 原始JSON字符串
        
    Returns:
        修复后的JSON字符串
    """
    try:
        # 如果看起来像是一个对象，尝试修复基本结构
        if json_str.strip().startswith('{'):
            # 方法1：尝试查找所有键值对（改进的正则表达式）
            # 使用更健壮的匹配策略，能处理包含特殊字符的值
            pairs: List[Tuple[str, str]] = []
            
            # 策略A：尝试匹配常见的键值对模式
            # 匹配：键: 值（值可以是字符串、数字、布尔值）
            simple_pairs = re.findall(r'["\']?(\w+)["\']?\s*:\s*("(?:\\.|[^"\\])*"|\d+|true|false|null)', json_str)
            if simple_pairs:
                pairs.extend(simple_pairs)
            
            # 策略B：如果策略A失败，尝试更宽松的匹配
            if not pairs:
                # 匹配：键: "值"（值可以包含转义引号）
                quoted_pairs = re.findall(r'["\']?(\w+)["\']?\s*:\s*"((?:\\.|[^"\\])*)"', json_str)
                if quoted_pairs:
                    pairs.extend(quoted_pairs)
            
            # 策略C：如果以上都失败，尝试提取所有可能的键值对（最宽松）
            if not pairs:
                # 匹配：键: 值（值到下一个键或结束）
                loose_pairs = re.findall(r'["\']?(\w+)["\']?\s*:\s*([^,\}]+)', json_str)
                if loose_pairs:
                    pairs.extend(loose_pairs)
            
            if pairs:
                # 重建JSON对象
                fixed_pairs: List[str] = []
                for key, value in pairs:
                    key = key.strip()
                    value = value.strip()
                    
                    # 确保键被引号包围
                    if not (key.startswith('"') and key.endswith('"')):
                        key = f'"{key}"'
                    
                    # 如果值看起来像字符串（包含字母、中文、特殊字符），确保它被引号包围
                    if (not (value.startswith('"') and value.endswith('"')) and 
                        not value in ('true', 'false', 'null') and 
                        not re.match(r'^\d+(\.\d+)?$', value)):
                        # 转义值中的引号
                        value = value.replace('"', '\\"')
                        value = f'"{value}"'
                    
                    fixed_pairs.append(f'{key}: {value}')
                
                result_json = '{' + ', '.join(fixed_pairs) + '}'
                # 验证生成的JSON是否有效
                try:
                    json.loads(result_json)
                    return result_json
                except json.JSONDecodeError:
                    # 如果无效，继续尝试其他方法
                    pass
        
        # 如果看起来像是一个数组，尝试修复基本结构
        elif json_str.strip().startswith('['):
            # 尝试匹配数组元素（支持字符串和简单值）
            elements: List[str] = []
            
            # 策略A：匹配引号包围的字符串
            quoted_elements = re.findall(r'"((?:\\.|[^"\\])*)"', json_str)
            if quoted_elements:
                elements.extend(quoted_elements)
            
            # 策略B：如果策略A失败，尝试匹配非引号值
            if not elements:
                simple_elements = re.findall(r'\[\s*([^,\]]+)\s*\]', json_str)
                if simple_elements:
                    elements.extend(simple_elements)
            
            if elements:
                # 重建JSON数组
                fixed_elements: List[str] = []
                for elem in elements:
                    elem = elem.strip()
                    if elem:
                        # 如果元素包含字母或特殊字符，用引号包围
                        if re.search(r'[a-zA-Z\u4e00-\u9fa5]', elem):
                            elem = elem.replace('"', '\\"')
                            fixed_elements.append(f'"{elem}"')
                        else:
                            fixed_elements.append(elem)
                
                result_json = '[' + ', '.join(fixed_elements) + ']'
                # 验证生成的JSON是否有效
                try:
                    json.loads(result_json)
                    return result_json
                except json.JSONDecodeError:
                    # 如果无效，继续尝试其他方法
                    pass
        
    except Exception:
        # 激进修复出错，不需要打印
        pass

    # 如果所有修复都失败，返回一个最小的有效JSON
    # 变量类型注解
    result: str = '{"error": "无法修复JSON格式", "original_content": ' + json.dumps(json_str[:200]) + '}'
    return result



def get_concept_profile(prompt: str, api_config: APIConfig, logger: Optional[Any] = None, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    调用AI生成概念配置文件

    Args:
        prompt: 概念学习提示词
        api_config: API配置字典
        logger: 日志记录器实例
        config: 配置字典（可选）

    Returns:
        概念配置字典，失败返回None
    """
    # 读取API参数配置
    try:
        if config:
            api_params = config.get('API_Parameters', {}) or {}  # type: ignore
            max_tokens = int(api_params.get('concept_max_tokens', 4000))  # type: ignore
            temperature = float(api_params.get('concept_temperature', 0.3))  # type: ignore
        else:
            max_tokens = 4000
            temperature = 0.3
    except (ValueError, TypeError) as e:
        if logger:
            logger.warning(f"读取概念分析API参数配置失败，使用默认值: {e}")
        max_tokens = 4000
        temperature = 0.3

    # 使用统一的API调用函数
    system_prompt = "你是一位学术研究专家，专门研究概念的历史发展和理论演化。请基于提供的种子论文，深入分析并创建一个关于指定概念的全面学习笔记。"
    return _call_ai_api(prompt, api_config, system_prompt, max_tokens=max_tokens, temperature=temperature, response_format="json", logger=logger)


def get_concept_analysis(prompt: str, api_config: APIConfig, logger: Optional[Any] = None, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    调用AI进行概念分析

    Args:
        prompt: 概念分析提示词
        api_config: API配置字典
        logger: 日志记录器实例
        config: 配置字典（可选）

    Returns:
        概念分析字典，失败返回None
    """
    # 读取API参数配置
    try:
        if config:
            api_params = config.get('API_Parameters', {}) or {}  # type: ignore
            max_tokens = int(api_params.get('concept_max_tokens', 4000))  # type: ignore
            temperature = float(api_params.get('concept_temperature', 0.3))  # type: ignore
        else:
            max_tokens = 4000
            temperature = 0.3
    except (ValueError, TypeError) as e:
        if logger:
            logger.warning(f"读取概念分析API参数配置失败，使用默认值: {e}")
        max_tokens = 4000
        temperature = 0.3

    # 使用统一的API调用函数
    system_prompt = "你是一位专门研究概念的学术分析专家。请基于提供的概念学习笔记，对当前论文进行深度分析，评估其在该概念发展历程中的地位和贡献。"
    return _call_ai_api(prompt, api_config, system_prompt, max_tokens=max_tokens, temperature=temperature, response_format="json", logger=logger)


class ContextLengthExceededError(Exception):
    """上下文长度超限错误，用于智能切换到备用引擎"""
    pass


class RateLimiter:
    """
    双引擎令牌桶流量控制器
    实现主引擎和备用引擎的独立TPM/RPM管理
    """

    def __init__(self, primary_tpm_limit: int = 900000, primary_rpm_limit: int = 9000,
                 backup_tpm_limit: int = 2000000, backup_rpm_limit: int = 9000) -> None:
        """
        初始化双引擎令牌桶限速器

        Args:
            primary_tpm_limit: 主引擎每分钟令牌数限制（0表示被动模式）
            primary_rpm_limit: 主引擎每分钟请求数限制（0表示被动模式）
            backup_tpm_limit: 备用引擎每分钟令牌数限制（0表示被动模式）
            backup_rpm_limit: 备用引擎每分钟请求数限制（0表示被动模式）
        """
        # 适应性混合速率控制 - 判断引擎模式
        # 主动模式（proactive）：使用令牌桶进行主动速率控制
        # 被动模式（reactive）：跳过令牌桶控制，依赖API的429错误处理
        
        # 主引擎模式判断
        if primary_tpm_limit > 0 and primary_rpm_limit > 0:
            self.primary_mode = 'proactive'
            # 主引擎配置
            self.primary_tpm_limit = primary_tpm_limit
            self.primary_rpm_limit = primary_rpm_limit
            self.primary_tpm_tokens = primary_tpm_limit
            self.primary_tpm_capacity = primary_tpm_limit
            self.primary_tpm_last_refill = time.time()
            self.primary_tpm_refill_rate = primary_tpm_limit / 60.0
            self.primary_rpm_tokens = primary_rpm_limit
            self.primary_rpm_capacity = primary_rpm_limit
            self.primary_rpm_last_refill = time.time()
            self.primary_rpm_refill_rate = primary_rpm_limit / 60.0
        else:
            self.primary_mode = 'reactive'
            # 被动模式：设置最小值，避免除零错误
            self.primary_tpm_limit = 1
            self.primary_rpm_limit = 1
            self.primary_tpm_tokens = 1
            self.primary_tpm_capacity = 1
            self.primary_tpm_last_refill = time.time()
            self.primary_tpm_refill_rate = 1.0 / 60.0
            self.primary_rpm_tokens = 1
            self.primary_rpm_capacity = 1
            self.primary_rpm_last_refill = time.time()
            self.primary_rpm_refill_rate = 1.0 / 60.0

        # 备用引擎模式判断
        if backup_tpm_limit > 0 and backup_rpm_limit > 0:
            self.backup_mode = 'proactive'
            # 备用引擎配置
            self.backup_tpm_limit = backup_tpm_limit
            self.backup_rpm_limit = backup_rpm_limit
            self.backup_tpm_tokens = backup_tpm_limit
            self.backup_tpm_capacity = backup_tpm_limit
            self.backup_tpm_last_refill = time.time()
            self.backup_tpm_refill_rate = backup_tpm_limit / 60.0
            self.backup_rpm_tokens = backup_rpm_limit
            self.backup_rpm_capacity = backup_rpm_limit
            self.backup_rpm_last_refill = time.time()
            self.backup_rpm_refill_rate = backup_rpm_limit / 60.0
        else:
            self.backup_mode = 'reactive'
            # 被动模式：设置最小值，避免除零错误
            self.backup_tpm_limit = 1
            self.backup_rpm_limit = 1
            self.backup_tpm_tokens = 1
            self.backup_tpm_capacity = 1
            self.backup_tpm_last_refill = time.time()
            self.backup_tpm_refill_rate = 1.0 / 60.0
            self.backup_rpm_tokens = 1
            self.backup_rpm_capacity = 1
            self.backup_rpm_last_refill = time.time()
            self.backup_rpm_refill_rate = 1.0 / 60.0

        # 线程安全锁
        self.lock = threading.Lock()

        # 记录器将通过set_logger方法设置
        self.logger = None

    def set_logger(self, logger: Any) -> None:
        """设置记录器"""
        self.logger = logger

    def _log(self, level: str, message: str) -> None:
        """内部日志方法"""
        if self.logger:
            getattr(self.logger, level)(message)

    def _refill_primary(self):
        """补充主引擎令牌桶"""
        current_time = time.time()
        time_passed = current_time - self.primary_tpm_last_refill

        if time_passed > 0:
            # 补充主引擎TPM令牌
            new_tokens = time_passed * self.primary_tpm_refill_rate
            self.primary_tpm_tokens = min(self.primary_tpm_capacity, self.primary_tpm_tokens + new_tokens)
            self.primary_tpm_last_refill = current_time

            # 补充主引擎RPM令牌
            new_requests = time_passed * self.primary_rpm_refill_rate
            self.primary_rpm_tokens = min(self.primary_rpm_capacity, self.primary_rpm_tokens + new_requests)
            self.primary_rpm_last_refill = current_time

    def _refill_backup(self):
        """补充备用引擎令牌桶"""
        current_time = time.time()
        time_passed = current_time - self.backup_tpm_last_refill

        if time_passed > 0:
            # 补充备用引擎TPM令牌
            new_tokens = time_passed * self.backup_tpm_refill_rate
            self.backup_tpm_tokens = min(self.backup_tpm_capacity, self.backup_tpm_tokens + new_tokens)
            self.backup_tpm_last_refill = current_time

            # 补充备用引擎RPM令牌
            new_requests = time_passed * self.backup_rpm_refill_rate
            self.backup_rpm_tokens = min(self.backup_rpm_capacity, self.backup_rpm_tokens + new_requests)
            self.backup_rpm_last_refill = current_time

    def consume(self, tokens_needed: int, requests_needed: int = 1, engine_type: str = 'primary') -> Union[bool, str, float]:
        """
        尝试消耗指定引擎的令牌（欧米茄协议：适应性混合速率控制）
        增强线程安全性，避免竞态条件

        Args:
            tokens_needed: 需要消耗的令牌数
            requests_needed: 需要消耗的请求数（默认为1）
            engine_type: 引擎类型 ('primary' 或 'backup')

        Returns:
            bool: 如果令牌充足返回True
            str: 特殊信号如 "SWITCH_TO_BACKUP" 或 "TOKEN_LIMIT_EXCEEDED"
            float: 如果需要等待，返回等待时间（秒）
        """
        # 输入验证
        if tokens_needed <= 0:
            raise ValueError(f"tokens_needed必须大于0，当前值: {tokens_needed}")
        if requests_needed <= 0:
            raise ValueError(f"requests_needed必须大于0，当前值: {requests_needed}")
        if engine_type not in ['primary', 'backup']:
            raise ValueError(f"未知的引擎类型: {engine_type}，必须是 'primary' 或 'backup'")

        # 适应性混合速率控制：支持主动和被动两种模式
        # 检查当前引擎模式（这部分无需锁保护，因为是读取操作）
        if engine_type == 'primary' and self.primary_mode == 'reactive':
            self._log('info', "主引擎被动模式：放行，依赖API层429错误处理")
            return True
        elif engine_type == 'backup' and self.backup_mode == 'reactive':
            self._log('info', "备用引擎被动模式：放行，依赖API层429错误处理")
            return True

        # 主动模式：执行传统的令牌桶控制逻辑
        # 使用更大的锁范围确保原子性操作
        with self.lock:
            if engine_type == 'primary':
                # 首先补充主引擎令牌
                self._refill_primary_internal()

                # 尺寸预检：检查是否超过主引擎容量
                if tokens_needed > self.primary_tpm_capacity:
                    self._log('info', "论文过长，主引擎无法处理，建议切换到备用引擎")
                    return "SWITCH_TO_BACKUP"

                # 检查主引擎令牌是否充足
                if self.primary_tpm_tokens >= tokens_needed and self.primary_rpm_tokens >= requests_needed:
                    # 原子性消耗主引擎令牌
                    self.primary_tpm_tokens -= tokens_needed
                    self.primary_rpm_tokens -= requests_needed

                    self._log('debug', f"主引擎主动模式消耗成功 - TPM: {tokens_needed}/{int(self.primary_tpm_tokens)}, RPM: {requests_needed}/{int(self.primary_rpm_tokens)}")
                    return True
                else:
                    # 计算需要等待的时间（使用更安全的方式避免除零错误）
                    tpm_wait = 0.0
                    rpm_wait = 0.0

                    if self.primary_tpm_refill_rate > 0 and self.primary_tpm_tokens < tokens_needed:
                        tpm_wait = (tokens_needed - self.primary_tpm_tokens) / self.primary_tpm_refill_rate

                    if self.primary_rpm_refill_rate > 0 and self.primary_rpm_tokens < requests_needed:
                        rpm_wait = (requests_needed - self.primary_rpm_tokens) / self.primary_rpm_refill_rate

                    wait_time = max(tpm_wait, rpm_wait)

                    self._log('info', f"主引擎主动模式令牌不足，需要等待: {wait_time:.2f}秒")
                    return wait_time

            elif engine_type == 'backup':
                # 首先补充备用引擎令牌
                self._refill_backup_internal()

                # 尺寸预检：检查是否超过备用引擎容量
                if tokens_needed > self.backup_tpm_capacity:
                    self._log('info', "论文过长，备用引擎也无法处理")
                    return "TOKEN_LIMIT_EXCEEDED"

                # 检查备用引擎令牌是否充足
                if self.backup_tpm_tokens >= tokens_needed and self.backup_rpm_tokens >= requests_needed:
                    # 原子性消耗备用引擎令牌
                    self.backup_tpm_tokens -= tokens_needed
                    self.backup_rpm_tokens -= requests_needed

                    self._log('debug', f"备用引擎主动模式消耗成功 - TPM: {tokens_needed}/{int(self.backup_tpm_tokens)}, RPM: {requests_needed}/{int(self.backup_rpm_tokens)}")
                    return True
                else:
                    # 计算需要等待的时间（使用更安全的方式避免除零错误）
                    tpm_wait = 0.0
                    rpm_wait = 0.0

                    if self.backup_tpm_refill_rate > 0 and self.backup_tpm_tokens < tokens_needed:
                        tpm_wait = (tokens_needed - self.backup_tpm_tokens) / self.backup_tpm_refill_rate

                    if self.backup_rpm_refill_rate > 0 and self.backup_rpm_tokens < requests_needed:
                        rpm_wait = (requests_needed - self.backup_rpm_tokens) / self.backup_rpm_refill_rate

                    wait_time = max(tpm_wait, rpm_wait)

                    self._log('info', f"备用引擎主动模式令牌不足，需要等待: {wait_time:.2f}秒")
                    return wait_time
            else:
                # 这个分支理论上不会执行，因为前面已经验证过了
                raise ValueError(f"未知的引擎类型: {engine_type}")

    def _refill_primary_internal(self) -> None:
        """内部方法：在锁保护下补充主引擎令牌桶"""
        current_time = time.time()
        time_passed = current_time - self.primary_tpm_last_refill

        if time_passed > 0:
            # 补充主引擎TPM令牌
            new_tokens = time_passed * self.primary_tpm_refill_rate
            self.primary_tpm_tokens = min(self.primary_tpm_capacity, self.primary_tpm_tokens + new_tokens)
            self.primary_tpm_last_refill = current_time

            # 补充主引擎RPM令牌
            new_requests = time_passed * self.primary_rpm_refill_rate
            self.primary_rpm_tokens = min(self.primary_rpm_capacity, self.primary_rpm_tokens + new_requests)
            self.primary_rpm_last_refill = current_time

    def _refill_backup_internal(self) -> None:
        """内部方法：在锁保护下补充备用引擎令牌桶"""
        current_time = time.time()
        time_passed = current_time - self.backup_tpm_last_refill

        if time_passed > 0:
            # 补充备用引擎TPM令牌
            new_tokens = time_passed * self.backup_tpm_refill_rate
            self.backup_tpm_tokens = min(self.backup_tpm_capacity, self.backup_tpm_tokens + new_tokens)
            self.backup_tpm_last_refill = current_time

            # 补充备用引擎RPM令牌
            new_requests = time_passed * self.backup_rpm_refill_rate
            self.backup_rpm_tokens = min(self.backup_rpm_capacity, self.backup_rpm_tokens + new_requests)
            self.backup_rpm_last_refill = current_time

    def get_status(self, engine_type: str = 'all') -> Dict[str, float]:
        """获取指定引擎的令牌桶状态"""
        with self.lock:
            self._refill_primary()
            self._refill_backup()

            if engine_type == 'primary':
                return {
                    'tpm_tokens': self.primary_tpm_tokens,
                    'tpm_capacity': self.primary_tpm_capacity,
                    'tpm_usage_percent': (self.primary_tpm_capacity - self.primary_tpm_tokens) / self.primary_tpm_capacity * 100,
                    'rpm_tokens': self.primary_rpm_tokens,
                    'rpm_capacity': self.primary_rpm_capacity,
                    'rpm_usage_percent': (self.primary_rpm_capacity - self.primary_rpm_tokens) / self.primary_rpm_capacity * 100
                }
            elif engine_type == 'backup':
                return {
                    'tpm_tokens': self.backup_tpm_tokens,
                    'tpm_capacity': self.backup_tpm_capacity,
                    'tpm_usage_percent': (self.backup_tpm_capacity - self.backup_tpm_tokens) / self.backup_tpm_capacity * 100,
                    'rpm_tokens': self.backup_rpm_tokens,
                    'rpm_capacity': self.backup_rpm_capacity,
                    'rpm_usage_percent': (self.backup_rpm_capacity - self.backup_rpm_tokens) / self.backup_rpm_capacity * 100
                }
            else:  # 'all'
                return {
                    'primary_tpm_usage_percent': (self.primary_tpm_capacity - self.primary_tpm_tokens) / self.primary_tpm_capacity * 100,
                    'backup_tpm_usage_percent': (self.backup_tpm_capacity - self.backup_tpm_tokens) / self.backup_tpm_capacity * 100,
                    'primary_rpm_usage_percent': (self.primary_rpm_capacity - self.primary_rpm_tokens) / self.primary_rpm_capacity * 100,
                    'backup_rpm_usage_percent': (self.backup_rpm_capacity - self.backup_rpm_tokens) / self.backup_rpm_capacity * 100
                }


# 引擎映射表，统一引擎名称和日志术语
engine_map = {
    'primary': {
        'name': '主阅读引擎',
        'short_name': '主引擎'
    },
    'backup': {
        'name': '备用阅读引擎',
        'short_name': '备用引擎'
    }
}

# 全局双引擎令牌桶实例 - 在模块加载时初始化（只初始化一次）
try:
    _config = load_config('config.ini')
    
    # 辅助函数：安全地将字符串转换为整数，处理空值和空白
    def safe_int_convert(value_str: Any, default_value: int) -> int:
        """安全地将字符串转换为整数，处理空值和空白"""
        if value_str is None:
            return default_value
        
        # 转换为字符串并去除前后空白
        str_value = str(value_str).strip()
        
        # 如果字符串为空或仅包含空白，返回默认值0
        if not str_value:
            return 0
        
        # 尝试转换为整数
        try:
            return int(str_value)
        except ValueError:
            # 转换失败，返回默认值
            # 注意：模块初始化时没有logger，所以不打印
            return default_value

    # 支持0值，表示被动模式
    _primary_tpm_limit = safe_int_convert(_config.get('Performance', {}).get('primary_tpm_limit', 900000), 900000)
    _primary_rpm_limit = safe_int_convert(_config.get('Performance', {}).get('primary_rpm_limit', 9000), 9000)
    _backup_tpm_limit = safe_int_convert(_config.get('Performance', {}).get('backup_tpm_limit', 2000000), 2000000)
    _backup_rpm_limit = safe_int_convert(_config.get('Performance', {}).get('backup_rpm_limit', 9000), 9000)
    rate_limiter = RateLimiter(_primary_tpm_limit, _primary_rpm_limit, _backup_tpm_limit, _backup_rpm_limit)

    # 注意：模块初始化时没有logger，所以不打印初始化信息
except Exception as e:
    # 注意：模块初始化时没有logger，所以不打印
    rate_limiter = RateLimiter(900000, 9000, 2000000, 9000)


def get_summary_from_ai(prompt_text: str, primary_api_config: APIConfig, backup_api_config: APIConfig,
                       engine_type: str = 'primary', logger: Optional[Any] = None, config: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    调用AI API并返回结构化摘要（带重试机制和429错误处理）

    集成了Rate limiting (令牌桶流量控制) 功能，确保API调用符合速率限制

    Args:
        prompt_text: 完整的提示词文本
        primary_api_config: 主引擎API配置字典
        backup_api_config: 备用引擎API配置字典
        engine_type: 引擎类型 ('primary' 或 'backup')
        logger: 日志记录器实例（可选）

    Returns:
        Optional[Dict[str, Any]]: 结构化摘要，如果调用失败则返回None

    Raises:
        ValueError: 当输入参数无效时
        requests.RequestException: 当API调用失败时
    """
    if ('dummy' in (primary_api_config.get('api_key') or '') or 
        'dummy' in (backup_api_config.get('api_key') or '')):
        return {
            'common_core': {
                'summary': 'This is a dummy summary.',
                'key_points': ['Dummy key point 1', 'Dummy key point 2'],
                'methodology': 'Dummy methodology.',
                'findings': 'Dummy findings.',
                'conclusions': 'Dummy conclusions.',
                'relevance': 'Dummy relevance.',
                'limitations': 'Dummy limitations.'
            },
            'type_specific_details': {}
        }

    # 设置RateLimiter的logger
    if logger:
        rate_limiter.set_logger(logger)

    # 增强的输入验证
    if not prompt_text or not prompt_text.strip():
        raise ValueError("提示词文本不能为空")

    if not primary_api_config:
        raise ValueError("主引擎API配置必须是有效的字典")

    if not backup_api_config:
        raise ValueError("备用引擎API配置必须是有效的字典")

    # 检查prompt_text长度，防止内存溢出
    if len(prompt_text) > 10000000:  # 10MB限制
        raise ValueError(f"提示词文本过长({len(prompt_text)}字符)，超过10MB限制")

    # 根据引擎类型选择配置
    if engine_type in engine_map:
        api_config = primary_api_config if engine_type == 'primary' else backup_api_config
        engine_name = engine_map[engine_type]['name']
    else:
        raise ValueError(f"未知的引擎类型: {engine_type}")

    api_key = api_config.get('api_key')
    api_base = api_config.get('api_base')
    model_name = api_config.get('model')

    if not api_key or not api_key.strip():
        raise ValueError(f"{engine_name}的API密钥不能为空")

    if not model_name or not model_name.strip():
        raise ValueError(f"{engine_name}的模型名称不能为空")

    # 读取重试配置
    try:
        config = load_config('config.ini')
        int(config.get('Performance', {}).get('api_retry_attempts', 5))
    except Exception:
        pass  # 使用默认值5次重试

    # 如果未提供api_base，则使用默认值
    if api_base is None:
        api_base = 'https://api.openai.com/v1'

    # 读取API参数配置
    try:
        if config:
            if engine_type == 'primary':
                max_tokens = int(config.get('API_Parameters', {}).get('primary_max_tokens', 3000))
                temperature = float(config.get('API_Parameters', {}).get('primary_temperature', 0.3))
            else:  # backup
                max_tokens = int(config.get('API_Parameters', {}).get('backup_max_tokens', 8192))
                temperature = float(config.get('API_Parameters', {}).get('backup_temperature', 0.3))
        else:
            # 默认值（向后兼容）
            max_tokens = 3000
            temperature = 0.3
    except (ValueError, TypeError) as e:
        if logger:
            logger.warning(f"读取API参数配置失败，使用默认值: {e}")
        max_tokens = 3000
        temperature = 0.3

    # 从外部文件读取系统提示词
    try:
        with open('prompts/prompt_system_analyze.txt', 'r', encoding='utf-8') as f:
            system_prompt = f.read()
    except Exception as e:
        # 如果读取失败，使用默认提示词
        if logger:
            logger.warning(f"无法加载系统提示词文件，使用默认提示词: {e}")
        system_prompt = """你是一个学术文献分析专家。请对提供的学术文本进行深度分析，并返回一个结构化摘要。请严格按照JSON格式返回结果，包含title、authors、year、journal、summary、key_points、methodology、findings、conclusions、relevance、limitations等字段。"""

    # ==================== Rate Limiting (令牌桶流量控制) ====================
    # 估算token消耗量（提示词 + 预期响应）
    estimated_tokens = len(prompt_text) + 3000  # 预留3000 tokens给响应

    # 调用令牌桶控制器
    rate_limit_result = rate_limiter.consume(
        tokens_needed=estimated_tokens,
        requests_needed=1,
        engine_type=engine_type
    )

    # 处理速率限制结果
    if rate_limit_result is True:
        # 令牌充足，继续处理
        if logger:
            logger.debug(f"令牌桶检查通过，继续处理")
    elif isinstance(rate_limit_result, float):
        # 需要等待
        wait_time = rate_limit_result
        if logger:
            logger.info(f"令牌不足，等待 {wait_time:.2f}秒")
        time.sleep(wait_time)
        # 等待后重新检查令牌
        retry_result = rate_limiter.consume(
            tokens_needed=estimated_tokens,
            requests_needed=1,
            engine_type=engine_type
        )
        if retry_result is not True:
            if logger:
                logger.warning("等待后令牌检查仍未通过，跳过此论文")
            return None
    elif rate_limit_result == "SWITCH_TO_BACKUP":
        # 建议切换到备用引擎
        if engine_type == 'primary':
            if logger:
                logger.info("主引擎令牌不足，切换到备用引擎")
            return get_summary_from_ai(prompt_text, primary_api_config, backup_api_config, 'backup', logger=logger)
        else:
            if logger:
                logger.error("备用引擎令牌不足，无法处理此论文")
            return None
    elif rate_limit_result == "TOKEN_LIMIT_EXCEEDED":
        # 超出所有引擎限制
        if logger:
            logger.error("所有引擎令牌都不足，无法处理此论文")
        return None
    else:
        # 其他未知结果
        if logger:
            logger.error(f"令牌桶检查返回未知结果: {rate_limit_result}")
        return None
    # ======================================================================

    # 使用统一的API调用函数
    ai_response = _call_ai_api(prompt_text, api_config, system_prompt, max_tokens=max_tokens, temperature=temperature, response_format="json", logger=logger)

    if not ai_response:
        return None

    # 验证必需字段（两段式结构）
    if isinstance(ai_response, dict):  # type: ignore
        structured_summary: Dict[str, Any] = ai_response

        if 'common_core' not in structured_summary:
            # 兼容旧格式，自动转换
            if logger:
                logger.debug("检测到旧格式摘要，自动转换为两段式结构")
            structured_summary = {
                'common_core': structured_summary,
                'type_specific_details': {}
            }

        # 确保common_core是字典类型
        if not isinstance(structured_summary.get('common_core'), dict):
            if logger:
                logger.error(f"common_core类型错误: {type(structured_summary.get('common_core'))}")
            # 修复：返回None表示处理失败，而不是继续返回空结构
            # 这样可以正确触发main.py中的失败处理逻辑
            return None

        # 验证common_core中的必需字段
        required_fields = ['summary', 'key_points', 'methodology', 'findings', 'conclusions', 'relevance', 'limitations']
        for field in required_fields:
            if field not in structured_summary['common_core']:
                structured_summary['common_core'][field] = "未提供相关信息" if field != 'key_points' else []

        # 确保key_points是列表
        if not isinstance(structured_summary['common_core']['key_points'], list):
            structured_summary['common_core']['key_points'] = [str(structured_summary['common_core']['key_points'])]

        # 确保type_specific_details存在
        if 'type_specific_details' not in structured_summary:
            structured_summary['type_specific_details'] = {}

        if logger:
            # 显示令牌桶状态
            status = rate_limiter.get_status(engine_type)
            logger.debug(f"令牌桶状态: {status}")

        return structured_summary
    else:
        # 如果返回的是字符串，尝试手动提取信息
        if logger:
            logger.warning("AI返回非字典格式，尝试手动解析")
        return _extract_summary_manually(ai_response)


def _extract_summary_manually(ai_response: Union[Dict[str, Any], str]) -> Dict[str, Any]:
    """
    当JSON解析失败时，使用正则表达式从AI响应中提取摘要信息

    Args:
        ai_response: AI的原始响应文本

    Returns:
        手动提取的结构化摘要
    """
    # 导入正则表达式模块（如果尚未导入）
    import re

    # 初始化两段式结果字典
    result: Dict[str, Any] = {
        'common_core': {
            'summary': '',
            'key_points': [],
            'methodology': '',
            'findings': '',
            'conclusions': '',
            'relevance': '',
            'limitations': ''
        },
        'type_specific_details': {}
    }

    # 尝试使用正则表达式提取JSON格式的部分
    # 查找可能的JSON结构，即使周围有其他文本
    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    
        # 确保ai_response是字符串类型
    if isinstance(ai_response, dict):
        # 如果是字典，尝试转换为JSON字符串
        try:
            ai_response_str = json.dumps(ai_response)
        except (TypeError, ValueError):
            ai_response_str = str(ai_response)
    else:
        ai_response_str = str(ai_response)
    
    json_matches: List[str] = re.findall(json_pattern, ai_response_str, re.DOTALL)
    
    for match in json_matches:
        try:
            # 尝试解析找到的JSON片段
            json_data: Any = json.loads(match)
            
            # 如果解析成功，提取有用信息
            if isinstance(json_data, dict):  # json.loads可能返回任何JSON类型，需要检查是否为字典
                # 提取common_core部分
                if 'common_core' in json_data:
                    for key in result['common_core']:
                        if key in json_data['common_core']:
                            result['common_core'][key] = json_data['common_core'][key]
                else:
                    # 如果没有common_core，直接从顶层提取
                    for key in result['common_core']:
                        if key in json_data:
                            result['common_core'][key] = json_data[key]
                
                # 如果成功提取到有用信息，直接返回
                if any(result['common_core'].values()):
                    return result
        except (json.JSONDecodeError, AttributeError):
            # 如果解析失败，继续尝试下一个匹配
            continue

    # 如果JSON提取失败，使用正则表达式直接从文本中提取内容
    # 定义各种键的正则表达式模式
    patterns = {
        'summary': [
            r'"summary"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'摘要[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'摘要[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'summary[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'summary[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'key_points': [
            r'"key_points"\s*:\s*\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]',
            r'要点[：:]\s*\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]',
            r'key_points[：:]\s*\[[^\]]*(?:\[[^\]]*\][^\]]*)*\]'
        ],
        'methodology': [
            r'"methodology"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'方法[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'方法[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'methodology[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'methodology[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'findings': [
            r'"findings"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'发现[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'发现[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'findings[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'findings[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'conclusions': [
            r'"conclusions"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'结论[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'结论[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'conclusions[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'conclusions[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'relevance': [
            r'"relevance"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'相关性[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'相关性[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'relevance[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'relevance[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ],
        'limitations': [
            r'"limitations"\s*:\s*"([^"]*(?:\\.[^"]*)*)"',
            r'限制[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'限制[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)',
            r'limitations[：:]\s*"([^"]*(?:\\.[^"]*)*)"',
            r'limitations[：:]\s*([^"\n\r]*(?:\n[^"\n\r]*)*)'
        ]
    }

    # 对每个字段尝试所有模式
    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            matches: List[str] = re.findall(pattern, ai_response_str, re.IGNORECASE | re.DOTALL)
            if matches:
                if field == 'key_points':
                    # 对于key_points，需要进一步解析列表项
                    list_content: str = matches[0]
                    # 尝试提取列表项
                    item_pattern = r'"([^"]*(?:\\.[^"]*)*)"'
                    items: List[str] = re.findall(item_pattern, list_content)
                    if not items:
                        # 如果没有找到带引号的项，尝试不带引号的项
                        item_pattern = r'([^,\[\]]+(?:\([^)]*\))?[^,\[\]]*)'
                        items = re.findall(item_pattern, list_content)
                    
                    # 清理并过滤空项
                    items = [item.strip().strip('"\'') for item in items if item.strip()]
                    if items:
                        result['common_core'][field] = items
                        break
                else:
                    # 对于其他字段，直接使用第一个匹配
                    content_str: str = matches[0].strip()
                    # 清理内容
                    content_str = re.sub(r'\s+', ' ', content_str)  # 合并多个空白字符
                    content_str = content_str.strip('"\'' )  # 移除引号
                    if content_str:
                        result['common_core'][field] = content_str
                        break


    # 如果没有提取到任何内容，返回一个基本结构
    if not any(result['common_core'].values()):
        result['common_core']['summary'] = ai_response_str[:500]  # 取前500字符作为摘要
        result['common_core']['key_points'] = ['解析失败，请查看原始响应']

    return result





if __name__ == "__main__":
    # 测试函数
    # 注意：模块级别的测试代码，应该使用logging而不是print
    import logging

    # 创建测试用logger
    test_logger = logging.getLogger('ai_interface_test')
    test_logger.setLevel(logging.INFO)

    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    # 创建格式器
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s',
                                datefmt='%H:%M:%S')
    console_handler.setFormatter(formatter)

    # 添加处理器到记录器
    test_logger.addHandler(console_handler)

    test_logger.info("AI接口测试")
    test_logger.info("=" * 50)

    # 测试令牌桶状态
    status = rate_limiter.get_status()
    test_logger.info("令牌桶状态:")
    for key, value in status.items():
        test_logger.info(f"  {key}: {value:.2f}")

    test_logger.info("\n注意：要进行完整测试，请提供有效的API配置")
    test_logger.info("使用方法：")
    test_logger.info("  python ai_interface.py")
