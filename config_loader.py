import configparser
import logging
import os
from typing import Dict, List, Optional

from config_validator import validate_all_config
from dotenv import load_dotenv  # type: ignore
from services.settings import ApplicationSettings, validate_config_keys


logger = logging.getLogger(__name__)


class ConfigDict(dict[str, Dict[str, str]]):
    """一个类似字典的配置对象，增加了对 getboolean 方法的支持。"""

    def getboolean(self, section: str, option: str, fallback: bool = False) -> bool:
        try:
            value = self.get(section, {}).get(option)
            if value is None:
                return fallback
            return str(value).lower() in ("true", "1", "t", "y", "yes")
        except Exception:
            return fallback


def load_config(config_path: str = "config.ini") -> ConfigDict:
    """
    读取配置文件并返回一个 ConfigDict 对象。
    优先从环境变量（.env 文件）读取 API 密钥，如果没有则使用配置文件中的值。
    """

    if not config_path:
        raise ValueError("配置文件路径必须是非空字符串")

    config_path = os.path.normpath(config_path)

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件 '{config_path}' 不存在")

    try:
        file_size = os.path.getsize(config_path)
        if file_size > 1024 * 1024:
            raise ValueError(f"配置文件过大({file_size}字节)，超过 1MB 限制")
    except OSError as exc:
        raise OSError(f"无法访问配置文件: {exc}")

    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise configparser.Error(f"读取配置文件失败: {exc}")
    except UnicodeDecodeError as exc:
        raise configparser.Error(f"配置文件编码错误，请使用 UTF-8 编码: {exc}")

    required_sections: List[str] = [
        "Application",
        "Paths",
        "Primary_Reader_API",
        "Backup_Reader_API",
        "Writer_API",
        "Runtime",
        "Validation",
        "Outline",
        "OutlineModels",
        "OutlineCostControl",
    ]
    missing_sections = [section for section in required_sections if section not in config.sections()]
    if missing_sections:
        raise configparser.Error(f"配置文件缺少必需的段: {', '.join(missing_sections)}")

    config_dict: Dict[str, Dict[str, str]] = {}
    for section_name in config.sections():
        section = config[section_name]
        config_dict[section_name] = {str(key): str(value) for key, value in section.items()}

    config_origin = os.path.dirname(os.path.abspath(config_path))
    for key, raw_value in list(config_dict.get("Paths", {}).items()):
        value = str(raw_value or "").strip()
        if not value or os.path.isabs(value):
            continue
        config_dict["Paths"][key] = os.path.abspath(os.path.join(config_origin, value))

    schema_errors = validate_config_keys(config_dict)
    if schema_errors:
        raise configparser.Error("配置文件包含不支持的字段: " + "; ".join(schema_errors))
    settings = ApplicationSettings.from_mutable_config(config_dict)
    if settings.validation.stage1_enabled or settings.validation.review_enabled:
        if "Validator_API" not in config.sections():
            raise configparser.Error(
                "配置文件错误：当启用验证功能时，必须提供 [Validator_API] 配置段。"
            )

    # 加载 .env 文件，使用项目根目录作为基础路径
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        load_dotenv(env_path)
        logger.info(f"从 {env_path} 加载环境变量")
    else:
        # 尝试在当前工作目录加载 .env 文件
        load_dotenv()
        logger.info("从当前工作目录加载环境变量")

    api_sections_dict: Dict[str, str] = {
        "Primary_Reader_API": "LLM_PRIMARY_READER_API",
        "Backup_Reader_API": "LLM_BACKUP_READER_API",
        "Writer_API": "LLM_WRITER_API",
        "Outline_API": "LLM_OUTLINE_API",
        "Free_Mode_API": "LLM_FREE_MODE_API",
        "Validator_API": "LLM_VALIDATOR_API",
    }

    for section_name, env_var in api_sections_dict.items():
        api_key_from_env: Optional[str] = os.getenv(env_var)
        if not api_key_from_env:
            continue
        if section_name in config_dict:
            config_dict[section_name]["api_key"] = api_key_from_env
            logger.info(f"从环境变量加载 {section_name}.api_key")
        else:
            logger.warning(f"环境变量 {env_var} 对应的配置段 [{section_name}] 不存在")

    try:
        valid, messages = validate_all_config(config_dict)
        if not valid:
            raise configparser.Error("配置文件验证失败: " + "; ".join(messages))
        for message in messages:
            logger.warning(message)
    except configparser.Error:
        raise
    except Exception as exc:
        raise configparser.Error(f"配置验证失败: {exc}") from exc

    return ConfigDict(config_dict)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    try:
        config = load_config()
        logger.info("配置加载成功:")
        for section, values in config.items():
            logger.info(f"[{section}]")
            for key, value in values.items():
                if key == "api_key" and value:
                    logger.info(f"  {key} = ********")
                else:
                    logger.info(f"  {key} = {value}")
    except Exception as exc:
        logger.error(f"配置加载失败: {exc}")
