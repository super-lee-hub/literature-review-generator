#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证系统诊断和优化工具
帮助检查验证功能问题并提供优化建议
"""

import json
import os
import configparser
from typing import Dict, Any, Optional

def check_validator_config():
    """检查验证配置"""
    print("🔍 检查验证系统配置...")
    
    config = configparser.ConfigParser()
    if not os.path.exists('config.ini'):
        print("❌ 未找到config.ini文件")
        return False
    
    config.read('config.ini', encoding='utf-8')
    
    # 检查验证配置
    validator_config = dict(config.items('Validator_API')) if config.has_section('Validator_API') else {}
    
    print("\n📋 验证API配置检查:")
    print(f"  API密钥: {'✅' if validator_config.get('api_key') else '❌'}")
    print(f"  模型: {validator_config.get('model', '未设置')}")
    print(f"  API地址: {validator_config.get('api_base', '未设置')}")
    
    # 检查性能配置
    performance_config = dict(config.items('Performance')) if config.has_section('Performance') else {}
    stage1_validation = performance_config.get('enable_stage1_validation', 'false')
    stage2_validation = performance_config.get('enable_stage2_validation', 'false')
    
    print(f"\n⚙️  验证开关配置:")
    print(f"  第一阶段验证: {'✅' if stage1_validation == 'true' else '❌'}")
    print(f"  第二阶段验证: {'✅' if stage2_validation == 'true' else '❌'}")
    
    # 检查API参数配置
    api_params = dict(config.items('API_Parameters')) if config.has_section('API_Parameters') else {}
    validator_max_tokens = api_params.get('validator_max_tokens', '4096')
    validator_temperature = api_params.get('validator_temperature', '0.3')
    
    print(f"\n🔧 验证API参数:")
    print(f"  最大令牌数: {validator_max_tokens}")
    print(f"  温度参数: {validator_temperature}")
    
    return True

def analyze_validation_results():
    """分析最近的验证结果"""
    print("\n📊 分析验证结果...")
    
    # 查找最新的输出目录
    output_dirs = []
    output_path = 'output'
    if os.path.exists(output_path):
        for item in os.listdir(output_path):
            item_path = os.path.join(output_path, item)
            if os.path.isdir(item_path):
                output_dirs.append((item, os.path.getmtime(item_path)))
    
    if not output_dirs:
        print("❌ 未找到输出目录")
        return False
    
    # 获取最新的项目
    latest_project = max(output_dirs, key=lambda x: x[1])[0]
    print(f"🎯 分析最新项目: {latest_project}")
    
    # 查找summaries.json文件
    summaries_file = os.path.join(output_path, latest_project, f'{latest_project}_summaries.json')
    if os.path.exists(summaries_file):
        try:
            with open(summaries_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            print(f"📄 找到 {len(data)} 篇论文的摘要数据")
            
            # 分析验证修正情况
            corrections_count = 0
            validated_count = 0
            
            for i, item in enumerate(data[:5]):  # 检查前5篇
                status = item.get('status', '')
                ai_summary = item.get('ai_summary', {})
                common_core = ai_summary.get('common_core', {})
                
                # 检查是否包含验证修正标记
                fields_to_check = ['findings', 'conclusions', 'relevance', 'limitations']
                for field in fields_to_check:
                    content = common_core.get(field, '')
                    if '[验证修正]' in content:
                        corrections_count += 1
                
                # 检查是否有验证标记
                for key, value in common_core.items():
                    if '[验证修正]' in str(value):
                        validated_count += 1
            
            print(f"🔍 验证修正统计:")
            print(f"  修正的字段数: {corrections_count}")
            print(f"  被验证的项目: {validated_count}")
            
            if corrections_count > 0:
                print("⚠️  检测到验证修正，建议检查验证逻辑是否过于严格")
            else:
                print("✅ 未检测到验证修正")
                
        except Exception as e:
            print(f"❌ 读取摘要文件失败: {e}")
    
    return True

def provide_optimization_advice():
    """提供优化建议"""
    print("\n💡 验证系统优化建议:")
    
    print("\n1. 🎯 如果验证过于严格:")
    print("   - 关闭第一阶段验证: enable_stage1_validation = false")
    print("   - 适用于大批量文献处理（>50篇）")
    print("   - 提升处理速度，减少误判")
    
    print("\n2. 🔧 如果验证效果不佳:")
    print("   - 更换验证模型为更强的模型（如GPT-4、Claude-3.5）")
    print("   - 调整验证温度参数（建议0.1-0.3）")
    print("   - 增加验证最大令牌数")
    
    print("\n3. ⚙️  如果模型切换后仍有问题:")
    print("   - 确保Validator_API使用了新的模型配置")
    print("   - 清理验证缓存: 删除output/[项目]/cache目录")
    print("   - 检查API密钥是否正确")
    
    print("\n4. 🧪 验证逻辑改进:")
    print("   - 使用改进的验证提示词（prompt_validate_analysis_improved.txt）")
    print("   - 区分事实错误和表述差异")
    print("   - 关注核心信息而非细节表述")

def test_validator_api():
    """测试验证API连接"""
    print("\n🧪 测试验证API连接...")
    
    try:
        from ai_interface import _call_ai_api
        from config_loader import load_config
        
        config = load_config('config.ini')
        validator_config = config.get('Validator_API', {})
        
        if not validator_config.get('api_key'):
            print("❌ 未配置验证API密钥")
            return False
        
        # 构建API配置
        api_config = {
            'api_key': validator_config.get('api_key'),
            'model': validator_config.get('model', ''),
            'api_base': validator_config.get('api_base', 'https://api.openai.com/v1')
        }
        
        # 测试调用
        system_prompt = "你是一个简单的验证器，只需要回复'验证测试成功'"
        test_prompt = "请回复'验证测试成功'"
        
        result = _call_ai_api(
            test_prompt,
            api_config,
            system_prompt,
            max_tokens=100,
            temperature=0.1
        )
        
        if result:
            print("✅ 验证API连接正常")
            return True
        else:
            print("❌ 验证API连接失败")
            return False
            
    except Exception as e:
        print(f"❌ 验证API测试出错: {e}")
        return False

def main():
    """主函数"""
    print("🔧 验证系统诊断工具")
    print("=" * 50)
    
    # 1. 检查配置
    config_ok = check_validator_config()
    
    # 2. 分析验证结果
    analyze_validation_results()
    
    # 3. 测试API连接
    if config_ok:
        test_validator_api()
    
    # 4. 提供优化建议
    provide_optimization_advice()
    
    print("\n" + "=" * 50)
    print("📋 诊断完成")

if __name__ == "__main__":
    main()