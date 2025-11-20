import configparser
import os
import logging
from typing import Dict, Optional, List
from config_validator import validate_all_config
from dotenv import load_dotenv  # type: ignore

# 设置模块级logger
logger = logging.getLogger(__name__)


class ConfigDict(dict[str, Dict[str, str]]):
    """一个类似字典的配置对象，增加了对getboolean方法的支持。"""
    def getboolean(self, section: str, option: str, fallback: bool = False) -> bool:
        """
        从类似字典的配置中获取布尔值。
        兼容 'true', '1', 't', 'y', 'yes' (不区分大小写)。
        """
        try:
            value = self.get(section, {}).get(option)
            if value is None:
                return fallback
            return str(value).lower() in ('true', '1', 't', 'y', 'yes')
        except Exception:
            return fallback


def load_config(config_path: str = 'config.ini') -> 'ConfigDict':
    """
    读取配置文件并返回一个ConfigDict对象。
    优先从环境变量(.env文件)读取API密钥，如果没有则使用配置文件中的值。

    Args:
        config_path: 配置文件路径，默认为 'config.ini'

    Returns:
        ConfigDict: 包含所有配置项的类字典对象

    Raises:
        FileNotFoundError: 当配置文件不存在时
        configparser.Error: 当配置文件格式错误时
    """
    if not config_path:
        raise ValueError("配置文件路径必须是非空字符串")

    # 规范化路径，防止路径遍历攻击
    config_path = os.path.normpath(config_path)

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件 '{config_path}' 不存在")

    # 检查文件大小，防止处理异常大的配置文件
    try:
        file_size: int = os.path.getsize(config_path)
        if file_size > 1024 * 1024:  # 1MB限制
            raise ValueError(f"配置文件过大({file_size}字节)，超过1MB限制")
    except OSError as e:
        raise OSError(f"无法访问配置文件: {e}")

    config: configparser.ConfigParser = configparser.ConfigParser()

    try:
        config.read(config_path, encoding='utf-8')
    except configparser.Error as e:
        raise configparser.Error(f"读取配置文件失败: {e}")
    except UnicodeDecodeError as e:
        raise configparser.Error(f"配置文件编码错误，请使用UTF-8编码: {e}")

    # 验证必需的配置段
    required_sections: List[str] = ['Paths', 'Primary_Reader_API', 'Backup_Reader_API', 'Writer_API']
    missing_sections: List[str] = [section for section in required_sections if section not in config.sections()]
    if missing_sections:
        raise configparser.Error(f"配置文件缺少必需的段: {', '.join(missing_sections)}")

    # 将配置转换为字典格式
    config_dict: Dict[str, Dict[str, str]] = {}
    for section_name in config.sections():
        config_dict[section_name] = dict(config[section_name])

    # 动态验证 Validator_API 段
    performance_section: Dict[str, str] = config_dict.get('Performance', {})
    stage1_enabled: bool = str(performance_section.get('enable_stage1_validation', 'false')).lower() == 'true'
    stage2_enabled: bool = str(performance_section.get('enable_stage2_validation', 'false')).lower() == 'true'

    if stage1_enabled or stage2_enabled:
        if 'Validator_API' not in config.sections():
            raise configparser.Error("配置文件错误：当启用验证功能 (enable_stage1_validation 或 enable_stage2_validation) 时，必须提供 [Validator_API] 配置段。")

    # ===== 使用标准化的 python-dotenv 加载环境变量 =====
    load_dotenv()  # 自动加载 .env 文件

    # 使用环境变量覆盖 API 密钥
    api_sections_dict: Dict[str, str] = {
        'Primary_Reader_API': 'LLM_PRIMARY_READER_API',
        'Backup_Reader_API': 'LLM_BACKUP_READER_API',
        'Writer_API': 'LLM_WRITER_API',
        'Validator_API': 'LLM_VALIDATOR_API'
    }
    
    for section_name, env_var in api_sections_dict.items():
        api_key_from_env: Optional[str] = os.getenv(env_var)
        if api_key_from_env:
            if section_name in config_dict:
                config_dict[section_name]['api_key'] = api_key_from_env
                logger.info(f"从环境变量加载 {section_name}.api_key")
            else:
                logger.warning(f"环境变量 {env_var} 对应的配置段 [{section_name}] 不存在")
    # ===================================================

    # 使用统一验证模块验证所有配置
    try:
        warnings_list: List[str]
        _, warnings_list = validate_all_config(config_dict)

        # 输出警告信息
        for warning in warnings_list:
            logger.warning(warning)

    except Exception as e:
        logger.warning(f"配置验证过程中发现问题: {e}")

    return ConfigDict(config_dict)


if __name__ == "__main__":
    # 测试函数
    import logging
    logger = logging.getLogger(__name__)
    try:
        config = load_config()
        logger.info("配置加载成功:")
        for section, values in config.items():
            logger.info(f"[{section}]")
            for key, value in values.items():
                logger.info(f"  {key} = {value}")
    except Exception as e:
        logger.error(f"配置加载失败: {e}")