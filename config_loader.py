import configparser
import logging
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

from config_validator import validate_all_config
from dotenv import load_dotenv  # type: ignore  # compatibility for legacy callers/tests
from services.credential_provenance import (
    CredentialProvenance,
    resolve_credentials,
)
from services.settings import ApplicationSettings, validate_config_keys


logger = logging.getLogger(__name__)


class ConfigDict(dict[str, Dict[str, str]]):
    """一个类似字典的配置对象，增加了对 getboolean 方法的支持。"""

    credential_provenance: tuple[CredentialProvenance, ...] = ()

    def getboolean(self, section: str, option: str, fallback: bool = False) -> bool:
        try:
            value = self.get(section, {}).get(option)
            if value is None:
                return fallback
            return str(value).lower() in ("true", "1", "t", "y", "yes")
        except Exception:
            return fallback


def provider_sections_for_stage_plan(
    config: Mapping[str, Mapping[str, Any]],
    *,
    requested_stages: Iterable[Any] | None,
    action: str,
    free_mode_enabled: bool = False,
) -> tuple[str, ...]:
    """Return only provider roles reachable from the durable StagePlan."""

    from runtime.stage_planning import build_stage_plan

    settings = ApplicationSettings.from_config(config)
    plan = build_stage_plan(
        action=action,
        requested_stages=requested_stages,
        validation_enabled=settings.review_validation_enabled(),
    )
    roles: list[str] = []
    stages = set(plan.requested_stages)
    if "analyze" in stages:
        roles.append("Primary_Reader_API")
        stage1 = config.get("Stage1_Input", {})
        primary_only = str(stage1.get("primary_reader_only") or "").strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not primary_only:
            roles.append("Backup_Reader_API")
    if "outline" in stages:
        roles.append("Outline_API")
    if "review" in stages:
        roles.append("Writer_API")
    if "validate" in stages:
        roles.append("Validator_API")
    if free_mode_enabled:
        roles.append("Free_Mode_API")
    return tuple(dict.fromkeys(roles))


def load_config(
    config_path: str = "config.ini",
    *,
    required_provider_sections: Iterable[str] | None = None,
    action: str | None = None,
    requested_stages: Iterable[Any] | None = None,
    free_mode_enabled: bool = False,
    allow_template_credentials: bool = False,
) -> ConfigDict:
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

    if "Multimodal" in config_dict:
        logger.warning(
            "[Multimodal] is deprecated and read only for migration; "
            "Stage1 vision capability now comes from Primary_Reader_API.model."
        )

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

    # Resolve secrets without mutating the process environment.  The selected
    # source is explicit and conflicting meaningful values fail closed.
    config_dict, credential_provenance = resolve_credentials(
        config_dict,
        config_path=config_path,
    )

    resolved_required = required_provider_sections
    if resolved_required is None and action is not None:
        resolved_required = provider_sections_for_stage_plan(
            config_dict,
            requested_stages=requested_stages,
            action=action,
            free_mode_enabled=free_mode_enabled,
        )

    try:
        try:
            valid, messages = validate_all_config(
                config_dict,
                required_provider_sections=(
                    tuple(resolved_required) if resolved_required is not None else None
                ),
                allow_template_credentials=allow_template_credentials,
            )
        except TypeError as exc:
            # A few integrations replace the validator with a legacy one-arg
            # callback.  Keep that seam compatible without swallowing real
            # validation failures from the production validator.
            if "unexpected keyword" not in str(exc):
                raise
            valid, messages = validate_all_config(config_dict)
        if not valid:
            raise configparser.Error("配置文件验证失败: " + "; ".join(messages))
        for message in messages:
            logger.warning(message)
    except configparser.Error:
        raise
    except Exception as exc:
        raise configparser.Error(f"配置验证失败: {exc}") from exc

    result = ConfigDict(config_dict)
    result.credential_provenance = tuple(credential_provenance)
    return result


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
