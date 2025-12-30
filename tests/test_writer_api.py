#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Writer_API连接测试脚本
"""

import requests
import time
import configparser
from typing import Dict, Any, Optional

def load_config() -> configparser.ConfigParser:
    """加载配置文件"""
    config = configparser.ConfigParser()
    config.read('config.ini', encoding='utf-8')
    return config

def test_writer_api():
    """测试Writer_API连接"""
    print("测试Writer_API连接...")
    
    config = load_config()
    
    # 获取Writer_API配置
    writer_config = dict(config.items('Writer_API')) if config.has_section('Writer_API') else {}
    api_key = writer_config.get('api_key', '')
    model = writer_config.get('model', '')
    api_base = writer_config.get('api_base', '')
    
    print(f"API密钥: {'已设置' if api_key else '未设置'}")
    print(f"模型: {model}")
    print(f"API地址: {api_base}")
    
    if not all([api_key, model, api_base]):
        print("错误: Writer_API配置不完整")
        return False
    
    # 构建请求
    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    # 简单的测试消息
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Please reply with 'test success', only these two words."
            }
        ],
        "temperature": 0.1,
        "max_tokens": 100
    }
    
    print(f"发送请求到: {url}")
    print(f"使用模型: {model}")
    
    try:
        start_time = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        end_time = time.time()
        
        print(f"响应时间: {end_time - start_time:.2f}秒")
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            try:
                response_data = response.json()
                content = response_data['choices'][0]['message']['content']
                print(f"成功! 响应内容: {content}")
                return True
            except Exception as e:
                print(f"JSON解析失败: {e}")
                print(f"原始响应: {response.text[:500]}")
                return False
        else:
            print(f"HTTP错误: {response.status_code}")
            print(f"错误信息: {response.text[:500]}")
            
            # 尝试获取更多错误信息
            try:
                error_data = response.json()
                if 'error' in error_data:
                    print(f"错误详情: {error_data['error']}")
            except:
                pass
            return False
            
    except requests.exceptions.Timeout:
        print("连接超时")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"连接错误: {e}")
        return False
    except Exception as e:
        print(f"未知错误: {e}")
        return False

def test_with_different_models() -> Optional[str]:
    """测试不同的模型名称"""
    print("\n测试不同的模型名称...")
    
    config = load_config()
    writer_config = dict(config.items('Writer_API')) if config.has_section('Writer_API') else {}
    api_key = writer_config.get('api_key', '')
    api_base = writer_config.get('api_base', '')
    
    if not api_key:
        print("错误: 没有API密钥")
        return
    
    # 可能的Gemini模型名称
    possible_models = [
        'gemini-3-pro-preview',  # 当前配置
        'gemini-pro',           # 通用Gemini Pro
        'gemini-1.5-pro',       # Gemini 1.5 Pro
        'gemini-1.5-pro-latest',
        'gemini-1.0-pro-latest',
        'gemini-2.0-flash-exp', # Gemini 2.0 Flash Experimental
    ]
    
    for model in possible_models:
        print(f"\n尝试模型: {model}")
        
        url = f"{api_base.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Please reply with 'test success'"
                }
            ],
            "temperature": 0.1,
            "max_tokens": 100
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=10)
            print(f"状态码: {response.status_code}")
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    content = response_data['choices'][0]['message']['content']
                    print(f"OK {model} 工作正常: {content[:50]}")
                    return model
                except:
                    print(f"- {model} 响应格式可能有问题")
            else:
                print(f"- {model} 失败: {response.status_code}")
                if response.status_code == 404:
                    print("  可能是模型名称错误")
                    
        except Exception as e:
            print(f"- {model} 错误: {e}")
    
    return None

def check_api_base():
    """检查API地址是否有效"""
    print("\n检查API地址...")
    
    config = load_config()
    writer_config = dict(config.items('Writer_API')) if config.has_section('Writer_API') else {}
    api_base = writer_config.get('api_base', '')
    
    if not api_base:
        print("错误: 没有API地址")
        return
    
    # 常见的Gemini API地址
    common_bases = [
        'https://aihubmix.com/v1',  # 当前配置
        'https://generativelanguage.googleapis.com/v1beta',  # 官方Google Gemini
        'https://api.openai.com/v1',  # OpenAI格式（如果使用代理）
    ]
    
    for base in common_bases:
        print(f"\n测试API地址: {base}")
        try:
            # 尝试简单的GET请求检查连接
            response = requests.get(base, timeout=5)
            print(f"响应状态: {response.status_code}")
        except Exception as e:
            print(f"连接错误: {e}")

def main():
    """主函数"""
    print("Writer_API连接诊断工具")
    print("=" * 60)
    
    # 1. 测试当前配置
    print("\n1. 测试当前配置:")
    success = test_writer_api()
    
    if not success:
        print("\n2. 尝试不同的模型名称:")
        working_model = test_with_different_models()
        
        if working_model:
            print(f"\n找到可用的模型: {working_model}")
            print("建议更新config.ini中的model设置")
    
    print("\n3. 检查API地址:")
    check_api_base()
    
    print("\n" + "=" * 60)
    print("诊断完成")
    
    if success:
        print("Writer_API连接正常")
    else:
        print("Writer_API连接失败，请检查上述错误信息")

if __name__ == "__main__":
    main()
