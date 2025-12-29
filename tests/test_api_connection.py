#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API连接测试脚本
测试配置文件中的各个API端点是否正常工作
"""

import sys
import os
import json
import requests
import time
from typing import Dict, Any, Optional

def load_config():
    """加载配置文件"""
    try:
        with open('config.ini', 'r', encoding='utf-8') as f:
            config = {}
            current_section = None
            
            for line in f:
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1]
                    config[current_section] = {}
                elif '=' in line and current_section:
                    key, value = line.split('=', 1)
                    config[current_section][key.strip()] = value.strip()
            
            return config
    except Exception as e:
        print(f"❌ 配置文件加载失败: {e}")
        return None

def test_api_connection(name: str, api_config: Dict[str, str], test_message: str = "你好，请回复'连接正常'"):
    """测试单个API连接"""
    print(f"\n🔍 测试 {name} API连接...")
    
    try:
        api_key = api_config.get('api_key', '')
        model = api_config.get('model', '')
        api_base = api_config.get('api_base', '')
        
        if not all([api_key, model, api_base]):
            print(f"❌ {name}: 配置不完整")
            return False
        
        # 构造请求
        url = f"{api_base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": test_message
                }
            ],
            "temperature": 0.1,
            "max_tokens": 100
        }
        
        print(f"📡 URL: {url}")
        print(f"🤖 Model: {model}")
        print(f"⏱️  发送请求...")
        
        start_time = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        end_time = time.time()
        
        print(f"⏰ 响应时间: {end_time - start_time:.2f}秒")
        print(f"📊 状态码: {response.status_code}")
        
        if response.status_code == 200:
            try:
                response_data = response.json()
                content = response_data['choices'][0]['message']['content']
                print(f"✅ {name}: 连接成功!")
                print(f"📝 响应内容: {content[:100]}...")
                return True
            except Exception as e:
                print(f"❌ {name}: JSON解析失败 - {e}")
                print(f"📄 原始响应: {response.text[:200]}...")
                return False
        else:
            print(f"❌ {name}: HTTP错误 - {response.status_code}")
            print(f"📄 错误信息: {response.text[:200]}...")
            return False
            
    except requests.exceptions.Timeout:
        print(f"❌ {name}: 连接超时")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"❌ {name}: 连接错误 - {e}")
        return False
    except Exception as e:
        print(f"❌ {name}: 未知错误 - {e}")
        return False

def main():
    """主测试函数"""
    print("🚀 开始API连接测试...")
    print("=" * 60)
    
    config = load_config()
    if not config:
        print("❌ 无法加载配置文件")
        return
    
    # 测试所有配置的API
    test_results = {}
    
    # 主引擎
    if 'Primary_Reader_API' in config:
        test_results['Primary_Reader_API'] = test_api_connection("主引擎", config['Primary_Reader_API'])
    
    # 备用引擎
    if 'Backup_Reader_API' in config:
        test_results['Backup_Reader_API'] = test_api_connection("备用引擎", config['Backup_Reader_API'])
    
    # 写作引擎
    if 'Writer_API' in config:
        test_results['Writer_API'] = test_api_connection("写作引擎", config['Writer_API'])
    
    # 验证引擎
    if 'Validator_API' in config:
        test_results['Validator_API'] = test_api_connection("验证引擎", config['Validator_API'])
    
    # 总结结果
    print("\n" + "=" * 60)
    print("📊 测试结果总结:")
    
    success_count = 0
    total_count = len(test_results)
    
    for api_name, result in test_results.items():
        status = "✅ 成功" if result else "❌ 失败"
        print(f"  {api_name}: {status}")
        if result:
            success_count += 1
    
    print(f"\n🎯 总体结果: {success_count}/{total_count} 个API连接成功")
    
    if success_count == total_count:
        print("🎉 所有API连接正常!")
    elif success_count > 0:
        print("⚠️  部分API连接失败，请检查失败的配置")
    else:
        print("💥 所有API连接失败，请检查网络和配置")

if __name__ == "__main__":
    main()