#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
运行setup向导的临时脚本
"""

import os
import configparser

def run_setup_wizard():
    """交互式安装向导"""
    print("=" * 60)
    print("llm_reviewer_generator 文献综述自动生成器 - 交互式安装向导")
    print("=" * 60)
    print()
    
    # 检查配置文件是否存在
    config_path = 'config.ini'
    if os.path.exists(config_path):
        print(f"发现已存在的配置文件: {config_path}")
        choice = input("是否要覆盖现有配置? (y/n): ").lower().strip()
        if choice != 'y':
            print("安装向导已取消")
            return
    
    # 创建配置目录
    config_dir = os.path.dirname(config_path)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir)
    
    # 收集配置信息
    config = {}
    
    print("\n请按提示输入配置信息:")
    print("-" * 40)
    
    # 路径配置
    print("\n【路径配置】")
    zotero_report = input("Zotero报告文件路径 (留空跳过): ").strip()
    if zotero_report:
        config['Paths'] = {
            'zotero_report': zotero_report,
            'library_path': input("Zotero库路径: ").strip(),
            'output_path': input("输出目录路径 (默认: ./output): ").strip() or './output'
        }
    else:
        config['Paths'] = {
            'library_path': input("Zotero库路径: ").strip(),
            'output_path': input("输出目录路径 (默认: ./output): ").strip() or './output'
        }
    
    # API配置 - 安全提示
    print("\n" + "=" * 60)
    print("🔒 安全配置说明")
    print("=" * 60)
    print("\n为了保护您的API密钥，请不要在配置文件中存储敏感信息。")
    print("请按以下步骤操作：\n")
    print("1. 复制 .env.example 文件并重命名为 .env")
    print("2. 在 .env 文件中填入您的API密钥")
    print("3. 系统将自动从 .env 文件加载API密钥\n")
    print("需要的API密钥变量：")
    print("  - LLM_PRIMARY_READER_API")
    print("  - LLM_BACKUP_READER_API")
    print("  - LLM_WRITER_API")
    print("  - LLM_VALIDATOR_API (可选，用于验证功能)")
    print("\n" + "=" * 60)

    # API配置 - 只询问模型信息，不询问密钥
    print("\n【主阅读引擎API配置】")
    config['Primary_Reader_API'] = {
        'api_key': 'loaded_from_.env_file',  # 占位符，提示从.env加载
        'model': input("模型名称 (如: Pro/moonshotai/Kimi-K2-Instruct-0905): ").strip(),
        'api_base': input("API Base URL (默认: https://api.siliconflow.cn/v1): ").strip() or 'https://api.siliconflow.cn/v1'
    }

    print("\n【备用阅读引擎API配置】")
    config['Backup_Reader_API'] = {
        'api_key': 'loaded_from_.env_file',  # 占位符，提示从.env加载
        'model': input("模型名称 (如: gemini-2.5-pro): ").strip(),
        'api_base': input("API Base URL (默认: https://api.videocaptioner.cn/v1): ").strip() or 'https://api.videocaptioner.cn/v1'
    }

    print("\n【写作引擎API配置】")
    config['Writer_API'] = {
        'api_key': 'loaded_from_.env_file',  # 占位符，提示从.env加载
        'model': input("模型名称 (如: gemini-2.5-pro): ").strip(),
        'api_base': input("API Base URL (默认: https://api.videocaptioner.cn/v1): ").strip() or 'https://api.videocaptioner.cn/v1'
    }
    
    # 性能配置
    print("\n【性能配置】")
    config['Performance'] = {
        'max_workers': input("最大工作线程数 (默认: 3): ").strip() or '3',
        'api_retry_attempts': input("API重试次数 (默认: 5): ").strip() or '5',
        'primary_tpm_limit': input("主引擎TPM限制 (0=被动模式, 默认: 900000): ").strip() or '900000',
        'primary_rpm_limit': input("主引擎RPM限制 (0=被动模式, 默认: 9000): ").strip() or '9000',
        'backup_tpm_limit': input("备用引擎TPM限制 (0=被动模式, 默认: 2000000): ").strip() or '2000000',
        'backup_rpm_limit': input("备用引擎RPM限制 (0=被动模式, 默认: 9000): ").strip() or '9000'
    }

    # 验证模块配置
    print("\n【验证模块配置】 (可选，但强烈推荐)")
    enable_stage1 = input("是否启用第一阶段（论文分析）的交叉验证? (y/n, 默认n): ").lower().strip()
    config['Performance']['enable_stage1_validation'] = 'true' if enable_stage1 == 'y' else 'false'

    enable_stage2 = input("是否启用第二阶段（综述内容）的引用验证? (y/n, 默认n): ").lower().strip()
    config['Performance']['enable_stage2_validation'] = 'true' if enable_stage2 == 'y' else 'false'

    if enable_stage1 == 'y' or enable_stage2 == 'y':
        print("\n【验证者AI引擎API配置】")
        config['Validator_API'] = {
            'api_key': 'loaded_from_.env_file',  # 占位符，提示从.env加载
            'model': input("模型名称 (推荐: gpt-4-turbo): ").strip(),
            'api_base': input("API Base URL (默认: https://api.openai.com/v1): ").strip() or 'https://api.openai.com/v1'
        }

    # API参数配置
    print("\n【API参数配置】 (可选，但推荐)")
    print("这些参数可以根据您的模型和需求进行调整")
    config['API_Parameters'] = {
        'primary_max_tokens': input("主阅读引擎最大令牌数 (默认: 3000): ").strip() or '3000',
        'primary_temperature': input("主阅读引擎温度 (默认: 0.3): ").strip() or '0.3',
        'backup_max_tokens': input("备用阅读引擎最大令牌数 (默认: 8192): ").strip() or '8192',
        'backup_temperature': input("备用阅读引擎温度 (默认: 0.3): ").strip() or '0.3',
        'concept_max_tokens': input("概念分析最大令牌数 (默认: 4000): ").strip() or '4000',
        'concept_temperature': input("概念分析温度 (默认: 0.3): ").strip() or '0.3',
        'writer_max_tokens': input("写作引擎最大令牌数 (默认: 8000): ").strip() or '8000',
        'writer_temperature': input("写作引擎温度 (默认: 0.5): ").strip() or '0.5',
        'validator_max_tokens': input("验证引擎最大令牌数 (默认: 4096): ").strip() or '4096',
        'validator_temperature': input("验证引擎温度 (默认: 0.3): ").strip() or '0.3',
        'claims_max_tokens': input("观点验证最大令牌数 (默认: 8192): ").strip() or '8192',
        'claims_temperature': input("观点验证温度 (默认: 0.3): ").strip() or '0.3'
    }

    # 样式配置
    print("\n【文档样式配置】")
    config['Styling'] = {
        'font_name': input("字体名称 (默认: Times New Roman): ").strip() or 'Times New Roman',
        'font_size_body': input("正文字体大小 (默认: 12): ").strip() or '12',
        'font_size_heading1': input("一级标题字体大小 (默认: 16): ").strip() or '16',
        'font_size_heading2': input("二级标题字体大小 (默认: 14): ").strip() or '14'
    }
    
    # 写入配置文件
    parser = configparser.ConfigParser()
    
    for section, values in config.items():  # type: ignore
        parser.add_section(section)  # type: ignore
        for key, value in values.items():  # type: ignore
            parser.set(section, key, value)  # type: ignore
    
    with open(config_path, 'w', encoding='utf-8') as f:
        parser.write(f)
    
    print(f"\n配置文件已保存到: {config_path}")
    print("\n" + "=" * 60)
    print("⚠️  重要提醒：API密钥配置")
    print("=" * 60)
    print("\n请确保您已经：")
    print("1. 创建了 .env 文件（可以复制 .env.example）")
    print("2. 在 .env 文件中填入了您的API密钥")
    print("3. .env 文件不会被提交到版本控制系统\n")
    print("安装向导完成！您现在可以运行程序了。")
    print("\n示例命令:")
    print("  python main.py --project-name \"我的研究\" --run-all")
    print("  python main.py --pdf-folder \"D:\\\\我的PDFs\" --run-all")

if __name__ == "__main__":
    run_setup_wizard()