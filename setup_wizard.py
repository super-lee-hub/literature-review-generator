#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Interactive setup wizard for CLI users."""

from __future__ import annotations

import configparser
import os
from typing import Dict, Mapping, Sequence

from services.configuration_service import (
    API_ENV_MAPPING,
    MINERU_ENV_KEYS,
    PROVIDER_PRESETS,
    ensure_config_sections,
    normalize_api_base,
    normalize_for_save,
    read_env_file,
    save_config_and_env,
)
from services.environment_service import (
    detect_runtime_environment,
    recommended_conda_activate_command,
    recommended_conda_create_command,
)

DEFAULT_MINERU_ENV_VALUES: Dict[str, str] = {
    "MINERU_BASE_URL": "https://mineru.net/api/v4",
    "MINERU_API_TOKEN": "",
    "MINERU_MODEL_VERSION": "vlm",
    "MINERU_UPLOAD_ENDPOINT": "/file-urls/batch",
    "MINERU_POLL_ENDPOINT_TEMPLATES": (
        "/extract-results/batch/{batch_id},"
        "/extract-results/{batch_id},"
        "/extract/task/{batch_id}"
    ),
    "MINERU_POLL_INTERVAL_SECONDS": "3",
    "MINERU_POLL_TIMEOUT_SECONDS": "900",
    "MINERU_REQUEST_MAX_RETRIES": "2",
    "MINERU_RETRY_BACKOFF_SECONDS": "1.5",
    "ALLOW_LOCAL_PARSE_FALLBACK": "true",
}

API_PARAMETER_PROMPTS: tuple[tuple[str, str], ...] = (
    ("timeout_seconds", "API 超时时间（秒）"),
    ("primary_max_tokens", "主阅读引擎 max_tokens"),
    ("primary_temperature", "主阅读引擎 temperature"),
    ("backup_max_tokens", "备用阅读引擎 max_tokens"),
    ("backup_temperature", "备用阅读引擎 temperature"),
    ("concept_max_tokens", "概念预热 max_tokens"),
    ("concept_temperature", "概念预热 temperature"),
    ("writer_max_tokens", "写作引擎 max_tokens"),
    ("writer_temperature", "写作引擎 temperature"),
    ("outline_max_tokens", "大纲引擎 max_tokens"),
    ("outline_temperature", "大纲引擎 temperature"),
    ("free_mode_max_tokens", "自由模式 max_tokens"),
    ("free_mode_temperature", "自由模式 temperature"),
    ("validator_max_tokens", "验证引擎 max_tokens"),
    ("validator_temperature", "验证引擎 temperature"),
    ("claims_max_tokens", "观点验证 max_tokens"),
    ("claims_temperature", "观点验证 temperature"),
)


def _prompt(label: str, default: str = "", allow_empty: bool = True) -> str:
    hint_parts: list[str] = []
    if default:
        hint_parts.append(default)
        if allow_empty:
            hint_parts.append("输入 - 清空")
    elif allow_empty:
        hint_parts.append("可留空")

    hint = f" [{' / '.join(hint_parts)}]" if hint_parts else ""

    while True:
        value = input(f"{label}{hint}: ").strip()
        if value == "-" and allow_empty:
            return ""
        if value:
            return value
        if default:
            return default
        if allow_empty:
            return ""
        print("该项不能为空，请重新输入。")


def _prompt_secret(label: str, current_value: str = "") -> str:
    hint = " [回车保留现有值 / 输入 - 清空]" if current_value else " [可留空]"
    value = input(f"{label}{hint}: ").strip()
    if value == "-" and current_value:
        return ""
    if value:
        return value
    return current_value


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{default_label}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "1", "true"}:
            return True
        if value in {"n", "no", "0", "false"}:
            return False
        print("请输入 y 或 n。")


def _prompt_provider(default: str = "custom") -> str:
    provider_keys = ", ".join(PROVIDER_PRESETS.keys())
    provider = _prompt(f"服务商预设 ({provider_keys})", default=default, allow_empty=False)
    return provider if provider in PROVIDER_PRESETS else "custom"


def _parse_bool(value: str | bool) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _guess_provider(api_base: str, fallback: str) -> str:
    if not api_base:
        return fallback if fallback in PROVIDER_PRESETS else "custom"

    normalized = normalize_api_base(api_base, provider="custom").lower()
    for provider, preset in PROVIDER_PRESETS.items():
        if provider == "custom":
            continue
        preset_base = normalize_api_base(preset.default_api_base, provider=provider).lower()
        if normalized == preset_base:
            return provider
    return fallback if fallback in PROVIDER_PRESETS else "custom"


def _load_existing_config_sections(config_path: str) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(config_path):
        return ensure_config_sections()

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    existing = {
        section_name: {key: value for key, value in parser.items(section_name)}
        for section_name in parser.sections()
    }
    return ensure_config_sections(existing)


def _collect_api_section(
    sections: Dict[str, Dict[str, str]],
    api_keys: Dict[str, str],
    section_name: str,
    title: str,
    default_provider: str,
) -> None:
    print(f"\n[{title}]")
    allow_fallback = section_name in {"Outline_API", "Free_Mode_API"}
    current_section = sections[section_name]
    provider_default = _guess_provider(current_section.get("api_base", ""), default_provider)
    provider = _prompt_provider(default=provider_default)

    model_label = "模型名称"
    api_base_label = "API Base URL"
    if section_name == "Outline_API":
        model_label = "模型名称（留空时回退到 Writer_API）"
        api_base_label = "API Base URL（留空时回退到 Writer_API）"
    elif section_name == "Free_Mode_API":
        model_label = "模型名称（留空时回退到 Outline_API）"
        api_base_label = "API Base URL（留空时回退到 Outline_API）"

    model = _prompt(
        model_label,
        default=current_section.get("model", ""),
        allow_empty=allow_fallback,
    )
    api_base = _prompt(
        api_base_label,
        default=current_section.get("api_base", ""),
        allow_empty=allow_fallback,
    )
    api_key = _prompt_secret(
        "API Key（将写入 .env）",
        current_value=api_keys.get(section_name, ""),
    )

    effective_provider = _guess_provider(api_base, provider) if api_base else provider
    current_section["provider"] = "custom" if allow_fallback and not api_base else effective_provider
    current_section["model"] = model
    current_section["api_base"] = (
        normalize_api_base(api_base, provider=effective_provider) if api_base else ""
    )
    api_keys[section_name] = api_key


def _collect_retry_section(
    section: Dict[str, str],
    title: str,
    include_enabled: bool = False,
) -> None:
    print(f"\n[{title}]")
    if include_enabled:
        section["enabled"] = "true" if _prompt_yes_no(
            "启用阶段二失败章节自动补跑",
            _parse_bool(section.get("enabled", "true")),
        ) else "false"
    section["max_retry_rounds"] = _prompt(
        "最大重试轮数",
        section["max_retry_rounds"],
        allow_empty=False,
    )
    section["base_retry_delay"] = _prompt(
        "基础重试等待时间（秒）",
        section["base_retry_delay"],
        allow_empty=False,
    )
    section["max_retry_delay"] = _prompt(
        "最大重试等待时间（秒）",
        section["max_retry_delay"],
        allow_empty=False,
    )


def _collect_preprocess_section(
    sections: Dict[str, Dict[str, str]],
    extra_env_values: Dict[str, str],
) -> None:
    preprocess = sections["Preprocess"]
    print("\n[PDF 预处理]")
    preprocess["enabled"] = "true" if _prompt_yes_no(
        "启用 PDF 预处理",
        _parse_bool(preprocess["enabled"]),
    ) else "false"
    preprocess["cache_dir"] = _prompt(
        "预处理缓存目录",
        preprocess["cache_dir"],
        allow_empty=False,
    )
    preprocess["parser_mode"] = _prompt(
        "解析模式（local / remote / hybrid）",
        preprocess["parser_mode"],
        allow_empty=False,
    )
    preprocess["primary_parser"] = _prompt(
        "主解析器（local / mineru_remote）",
        preprocess["primary_parser"],
        allow_empty=False,
    )
    preprocess["fallback_parser"] = _prompt(
        "回退解析器（local / mineru_remote）",
        preprocess["fallback_parser"],
        allow_empty=False,
    )
    preprocess["extractor_profile"] = _prompt(
        "本地提取策略（auto / fitz / pymupdf4llm）",
        preprocess["extractor_profile"],
        allow_empty=False,
    )
    preprocess["ocr_mode"] = _prompt(
        "OCR 模式（auto / off / always）",
        preprocess["ocr_mode"],
        allow_empty=False,
    )
    preprocess["ocr_languages"] = _prompt(
        "OCR 语言包",
        preprocess["ocr_languages"],
        allow_empty=False,
    )
    preprocess["force_rebuild"] = "true" if _prompt_yes_no(
        "每次运行都强制重建预处理缓存",
        _parse_bool(preprocess["force_rebuild"]),
    ) else "false"
    preprocess["use_markdown_as_stage1_input"] = "true" if _prompt_yes_no(
        "阶段一默认使用 normalized.md 作为输入",
        _parse_bool(preprocess["use_markdown_as_stage1_input"]),
    ) else "false"
    preprocess["retain_structured_output"] = "true" if _prompt_yes_no(
        "保留 structured.json",
        _parse_bool(preprocess["retain_structured_output"]),
    ) else "false"
    preprocess["retain_page_index"] = "true" if _prompt_yes_no(
        "保留 page_index.json",
        _parse_bool(preprocess["retain_page_index"]),
    ) else "false"
    preprocess["retain_diagnostics"] = "true" if _prompt_yes_no(
        "保留 diagnostics.json",
        _parse_bool(preprocess["retain_diagnostics"]),
    ) else "false"
    preprocess["enable_local_rag"] = "true" if _prompt_yes_no(
        "启用本地 RAG 索引（可选）",
        _parse_bool(preprocess["enable_local_rag"]),
    ) else "false"
    if preprocess["enable_local_rag"] == "true":
        preprocess["rag_backend"] = _prompt(
            "本地 RAG 后端",
            preprocess["rag_backend"],
            allow_empty=False,
        )

    uses_remote_parser = (
        preprocess["parser_mode"] in {"remote", "hybrid"}
        or preprocess["primary_parser"] == "mineru_remote"
        or preprocess["fallback_parser"] == "mineru_remote"
    )
    has_existing_mineru = any(extra_env_values.get(key, "") for key in MINERU_ENV_KEYS)
    should_configure_mineru = _prompt_yes_no(
        "配置 MinerU 远程解析参数",
        uses_remote_parser or has_existing_mineru,
    )

    if not should_configure_mineru:
        return

    print("\n[MinerU 远程解析]")
    extra_env_values["MINERU_BASE_URL"] = _prompt(
        "MinerU Base URL",
        extra_env_values["MINERU_BASE_URL"],
        allow_empty=False,
    )
    extra_env_values["MINERU_API_TOKEN"] = _prompt_secret(
        "MinerU API Token",
        current_value=extra_env_values["MINERU_API_TOKEN"],
    )
    extra_env_values["MINERU_MODEL_VERSION"] = _prompt(
        "MinerU 模型版本",
        extra_env_values["MINERU_MODEL_VERSION"],
        allow_empty=False,
    )
    extra_env_values["MINERU_UPLOAD_ENDPOINT"] = _prompt(
        "上传接口路径",
        extra_env_values["MINERU_UPLOAD_ENDPOINT"],
        allow_empty=False,
    )
    extra_env_values["MINERU_POLL_ENDPOINT_TEMPLATES"] = _prompt(
        "轮询接口模板（逗号分隔）",
        extra_env_values["MINERU_POLL_ENDPOINT_TEMPLATES"],
        allow_empty=False,
    )
    extra_env_values["MINERU_POLL_INTERVAL_SECONDS"] = _prompt(
        "轮询间隔（秒）",
        extra_env_values["MINERU_POLL_INTERVAL_SECONDS"],
        allow_empty=False,
    )
    extra_env_values["MINERU_POLL_TIMEOUT_SECONDS"] = _prompt(
        "轮询超时（秒）",
        extra_env_values["MINERU_POLL_TIMEOUT_SECONDS"],
        allow_empty=False,
    )
    extra_env_values["MINERU_REQUEST_MAX_RETRIES"] = _prompt(
        "请求最大重试次数",
        extra_env_values["MINERU_REQUEST_MAX_RETRIES"],
        allow_empty=False,
    )
    extra_env_values["MINERU_RETRY_BACKOFF_SECONDS"] = _prompt(
        "请求退避时间（秒）",
        extra_env_values["MINERU_RETRY_BACKOFF_SECONDS"],
        allow_empty=False,
    )
    extra_env_values["ALLOW_LOCAL_PARSE_FALLBACK"] = "true" if _prompt_yes_no(
        "远程解析失败时允许回退到本地解析",
        _parse_bool(extra_env_values["ALLOW_LOCAL_PARSE_FALLBACK"]),
    ) else "false"


def _collect_validation_section(
    sections: Dict[str, Dict[str, str]],
    api_keys: Dict[str, str],
    existing_env: Mapping[str, str],
) -> None:
    validation = sections["Validation"]
    print("\n[验证设置]")
    stage1_validation = _prompt_yes_no(
        "启用阶段一验证",
        _parse_bool(validation.get("stage1_enabled", "false")),
    )
    review_validation = _prompt_yes_no(
        "启用综述验证",
        _parse_bool(validation.get("review_enabled", "true")),
    )
    validation["stage1_enabled"] = "true" if stage1_validation else "false"
    validation["review_enabled"] = "true" if review_validation else "false"

    if stage1_validation or review_validation:
        _collect_api_section(
            sections,
            api_keys,
            "Validator_API",
            "验证引擎",
            "openai",
        )
        return

    api_keys["Validator_API"] = existing_env.get("LLM_VALIDATOR_API", "")


def _collect_api_parameters(section: Dict[str, str]) -> None:
    print("\n[高级 API 参数]")
    for key, label in API_PARAMETER_PROMPTS:
        section[key] = _prompt(label, section[key], allow_empty=False)


def _collect_simple_fields(section: Dict[str, str], title: str, fields: Sequence[tuple[str, str]]) -> None:
    print(f"\n[{title}]")
    for key, label in fields:
        section[key] = _prompt(label, section[key], allow_empty=False)


def run_setup_wizard(config_path: str = "config.ini", env_path: str = ".env") -> None:
    """Run the interactive setup wizard and persist config/.env."""

    runtime = detect_runtime_environment()
    existing_env = read_env_file(env_path)
    sections = _load_existing_config_sections(config_path)
    api_keys = {
        section_name: existing_env.get(env_key, "")
        for section_name, env_key in API_ENV_MAPPING.items()
    }
    extra_env_values = {
        key: existing_env.get(key, DEFAULT_MINERU_ENV_VALUES[key])
        for key in MINERU_ENV_KEYS
    }

    print("=" * 72)
    print("auto-generate 配置向导")
    print("=" * 72)
    print("这个向导会同时生成 config.ini 和 .env。API Key 与 MinerU Token 会写入 .env。")
    if os.path.exists(config_path) or os.path.exists(env_path):
        print("检测到现有配置，直接回车会保留当前值；输入 - 可以清空可选项。")

    print("\n[当前运行环境]")
    print(f"解释器环境: {runtime.display_name}")
    print(f"解释器路径: {runtime.executable}")
    if runtime.needs_isolation_recommendation:
        print("建议使用独立 conda 环境，避免项目依赖与现有环境发生冲突。")
        print(f"  {recommended_conda_create_command()}")
        print(f"  {recommended_conda_activate_command()}")

    print("\n[路径配置]")
    sections["Paths"]["zotero_report"] = _prompt(
        "Zotero 报告路径（PDF 文件夹模式可留空）",
        sections["Paths"]["zotero_report"],
    )
    sections["Paths"]["library_path"] = _prompt(
        "Zotero 库路径（PDF 文件夹模式可留空）",
        sections["Paths"]["library_path"],
    )
    sections["Paths"]["output_path"] = _prompt(
        "输出目录",
        sections["Paths"]["output_path"],
        allow_empty=False,
    )

    _collect_api_section(sections, api_keys, "Primary_Reader_API", "主阅读引擎", "siliconflow")
    _collect_api_section(sections, api_keys, "Backup_Reader_API", "备用阅读引擎", "videocaptioner")
    _collect_api_section(sections, api_keys, "Writer_API", "写作引擎", "videocaptioner")
    _collect_api_section(sections, api_keys, "Outline_API", "大纲引擎", "videocaptioner")
    _collect_api_section(sections, api_keys, "Free_Mode_API", "自由模式对话引擎", "videocaptioner")

    _collect_simple_fields(
        sections["Runtime"],
        "运行设置",
        (
            ("max_workers", "最大并发数"),
            ("transport_retries", "传输层重试次数"),
            ("node_retry_limit", "节点重试上限"),
            ("total_job_deadline_seconds", "任务总时限（秒，0 表示不限制）"),
        ),
    )
    _collect_retry_section(sections["Retry_Settings"], "阶段一失败论文自动重试")
    _collect_retry_section(
        sections["Stage2_Retry"],
        "阶段二失败章节自动补跑",
        include_enabled=True,
    )
    _collect_preprocess_section(sections, extra_env_values)
    _collect_validation_section(sections, api_keys, existing_env)
    _collect_simple_fields(
        sections["GUI"],
        "GUI 设置",
        (("language", "界面语言（如 zh-CN / en）"),),
    )
    _collect_simple_fields(
        sections["Styling"],
        "文档样式",
        (
            ("font_name", "字体名称"),
            ("font_size_body", "正文字号"),
            ("font_size_heading1", "一级标题字号"),
            ("font_size_heading2", "二级标题字号"),
        ),
    )
    _collect_api_parameters(sections["API_Parameters"])

    normalize_for_save(sections)
    save_config_and_env(
        sections,
        api_keys,
        extra_env_values=extra_env_values,
        config_path=config_path,
        env_path=env_path,
    )

    print("\n配置已写入：")
    print(f"- {config_path}")
    print(f"- {env_path}")
    print("\n现在可以继续使用命令行，或运行 launch_gui.py / start_gui.bat 打开图形界面。")


if __name__ == "__main__":
    run_setup_wizard()
