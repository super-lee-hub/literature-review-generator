#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validator diagnostics helper aligned to canonical summaries."""

from __future__ import annotations

import configparser
import json
import os
from typing import Any, Dict

from models import APIConfig
from summary_schema import get_core_analysis, get_quality_audit, get_routing


def check_validator_config() -> bool:
    """Inspect validator-related configuration."""
    print("检查验证系统配置...")
    config = configparser.ConfigParser()
    if not os.path.exists("config.ini"):
        print("未找到 config.ini 文件")
        return False

    config.read("config.ini", encoding="utf-8")
    validator_config = dict(config.items("Validator_API")) if config.has_section("Validator_API") else {}
    performance_config = dict(config.items("Performance")) if config.has_section("Performance") else {}
    api_params = dict(config.items("API_Parameters")) if config.has_section("API_Parameters") else {}

    print("\n验证 API 配置")
    print(f"  API 密钥: {'✅' if validator_config.get('api_key') else '❌'}")
    print(f"  模型: {validator_config.get('model', '未设置')}")
    print(f"  API 地址: {validator_config.get('api_base', '未设置')}")

    print("\n验证开关配置")
    print(f"  阶段一验证: {'✅' if performance_config.get('enable_stage1_validation', 'false') == 'true' else '❌'}")
    print(f"  阶段二验证: {'✅' if performance_config.get('enable_stage2_validation', 'false') == 'true' else '❌'}")

    print("\n验证 API 参数")
    print(f"  validator_max_tokens: {api_params.get('validator_max_tokens', '4096')}")
    print(f"  validator_temperature: {api_params.get('validator_temperature', '0.3')}")
    return True


def analyze_validation_results() -> bool:
    """Analyze the latest project summaries and validation traces."""
    print("\n分析验证结果...")
    output_root = "output"
    if not os.path.exists(output_root):
        print("未找到 output 目录")
        return False

    projects = []
    for item in os.listdir(output_root):
        item_path = os.path.join(output_root, item)
        if os.path.isdir(item_path):
            projects.append((item, os.path.getmtime(item_path)))

    if not projects:
        print("未找到任何项目输出目录")
        return False

    latest_project = max(projects, key=lambda pair: pair[1])[0]
    summaries_file = os.path.join(output_root, latest_project, f"{latest_project}_summaries.json")
    print(f"分析最新项目: {latest_project}")

    if not os.path.exists(summaries_file):
        print("未找到 summaries.json")
        return False

    try:
        with open(summaries_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        print(f"读取摘要文件失败: {exc}")
        return False

    print(f"找到 {len(data)} 篇论文的摘要数据")
    corrections_count = 0
    validated_count = 0
    manual_review_count = 0

    for item in data[:5]:
        core = get_core_analysis(item)
        routing = get_routing(item)
        quality = get_quality_audit(item)

        if quality.get("needs_manual_review"):
            manual_review_count += 1

        for field in ["findings", "conclusions", "relevance", "limitations"]:
            content = core.get(field, "") or ""
            if "[验证修正]" in str(content):
                corrections_count += 1

        for value in core.values():
            if "[验证修正]" in str(value):
                validated_count += 1

        print(
            f"  - type={routing.get('paper_type') or 'null'}, "
            f"status={routing.get('classification_status')}, "
            f"manual_review={bool(quality.get('needs_manual_review'))}"
        )

    print("验证修正统计:")
    print(f"  修正字段数: {corrections_count}")
    print(f"  出现验证标记的项目数: {validated_count}")
    print(f"  建议人工复核数: {manual_review_count}")
    return True


def provide_optimization_advice() -> None:
    """Print validator tuning suggestions."""
    print("\n验证系统优化建议:")
    print("1. 如果验证过于严格，可关闭 enable_stage1_validation。")
    print("2. 如果验证质量不足，优先检查 Validator_API 的模型与密钥配置。")
    print("3. 如果切换模型后结果异常，检查 output/<项目>/cache 是否需要清理。")
    print("4. 现在摘要结构已是 canonical-first，诊断时优先看 routing/core_analysis/quality_audit。")


def check_validator_api() -> bool:
    """Smoke-test validator API connectivity."""
    print("\n测试验证 API 连接...")
    try:
        from ai_interface import _call_ai_api
        from config_loader import load_config

        config = load_config("config.ini")
        validator_config = config.get("Validator_API", {})
        if not validator_config.get("api_key"):
            print("未配置验证 API 密钥")
            return False

        api_config: APIConfig = {
            "api_key": validator_config.get("api_key"),
            "model": validator_config.get("model", ""),
            "api_base": validator_config.get("api_base", "https://api.openai.com/v1"),
        }
        result = _call_ai_api(
            "请只返回“验证测试成功”",
            api_config,
            "你是一个简洁的连通性测试助手。",
            max_tokens=50,
            temperature=0.1,
        )
        if result:
            print("验证 API 连接正常")
            return True
        print("验证 API 连接失败")
        return False
    except Exception as exc:
        print(f"验证 API 测试出错: {exc}")
        return False


def main() -> None:
    print("验证系统诊断工具")
    print("=" * 50)
    config_ok = check_validator_config()
    analyze_validation_results()
    if config_ok:
        check_validator_api()
    provide_optimization_advice()
    print("\n" + "=" * 50)
    print("诊断完成")


if __name__ == "__main__":
    main()
