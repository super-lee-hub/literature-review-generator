#!/usr/bin/env python3
"""
占位符清理工具 - 分析和清理文献综述中的占位符内容
"""

import os
import json
# from context_manager import PlaceholderAnalyzer  # Not used
from typing import Dict, Any  # List not used


def quick_placeholder_check(file_path: str) -> Dict[str, Any]:
    """快速检查JSON文件中的占位符情况"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        return {"error": f"无法读取文件: {str(e)}"}
    
    # 简单的占位符检查
    placeholder_keywords = [
        "未提供相关信息", "未提及", "未提供", "无相关信息", "未知",
        "Not provided", "N/A", "null", "None", "...", "无摘要", "无数据"
    ]
    
    result = {  # type: ignore
        "file_path": file_path,
        "total_papers": len(data),
        "placeholder_papers": 0,
        "placeholder_examples": []
    }
    
    for i, paper in enumerate(data):
        paper_info = paper.get('paper_info', {})
        ai_summary = paper.get('ai_summary', {})
        common_core = ai_summary.get('common_core', {}) if ai_summary else {}  # type: ignore
        
        # 检查是否包含占位符
        has_placeholder = False
        placeholder_fields = []
        
        # 检查基本字段
        for field in ['title', 'year', 'authors', 'journal']:
            value = paper_info.get(field, '')
            if value and any(keyword in str(value) for keyword in placeholder_keywords):
                has_placeholder = True
                placeholder_fields.append(f"{field}: {value}")  # type: ignore
        
        # 检查核心内容字段
        for field in ['summary', 'findings', 'methodology', 'conclusions']:
            value = common_core.get(field, '')  # type: ignore
            if value and any(keyword in str(value) for keyword in placeholder_keywords):  # type: ignore
                has_placeholder = True
                placeholder_fields.append(f"{field}: {value[:50]}...")  # type: ignore
        
        # 检查key_points
        key_points = common_core.get('key_points', [])  # type: ignore
        if isinstance(key_points, list):  # type: ignore
            for kp in key_points:  # type: ignore
                if kp and any(keyword in str(kp) for keyword in placeholder_keywords):  # type: ignore
                    has_placeholder = True
                    placeholder_fields.append(f"key_points: {kp}")  # type: ignore
        
        if has_placeholder:
            result["placeholder_papers"] += 1  # type: ignore
            result["placeholder_examples"].append({  # type: ignore
                "index": i,
                "title": paper_info.get('title', '未知标题'),
                "fields": placeholder_fields[:3]  # 只记录前3个例子
            })
    
    return result  # type: ignore


def main():
    """主函数"""
    print("🔍 开始快速占位符检查...")
    
    # 查找所有summaries.json文件
    summaries_files = []
    for root, dirs, files in os.walk("output"):  # type: ignore
        for file in files:
            if file.endswith("_summaries.json"):
                summaries_files.append(os.path.join(root, file))  # type: ignore
    
    if not summaries_files:
        print("❌ 未找到任何summaries.json文件")
        return
    
    print(f"📁 找到 {len(summaries_files)} 个JSON文件")  # type: ignore
    
    total_placeholders = 0
    total_papers = 0
    
    for file_path in summaries_files:  # type: ignore
        print(f"\n📄 检查: {file_path}")
        result = quick_placeholder_check(file_path)  # type: ignore
        
        if "error" in result:
            print(f"❌ {result['error']}")
            continue
        
        total_papers += result["total_papers"]  # type: ignore
        total_placeholders += result["placeholder_papers"]  # type: ignore
        
        placeholder_rate = (result["placeholder_papers"] / result["total_papers"]) * 100 if result["total_papers"] > 0 else 0  # type: ignore
        
        print(f"  📊 总论文: {result['total_papers']}")
        print(f"  ⚠️  占位符论文: {result['placeholder_papers']}")
        print(f"  📈 占位符比例: {placeholder_rate:.1f}%")
        
        if result["placeholder_examples"]:
            print("  🔍 占位符示例:")
            for example in result["placeholder_examples"][:2]:  # 只显示前2个
                print(f"    - 论文{example['index']+1}: {example['title']}")
                for field in example["fields"][:2]:  # 只显示前2个字段
                    print(f"      * {field}")
    
    print(f"\n📋 总体统计")
    print(f"📄 总论文数: {total_papers}")
    print(f"⚠️  占位符论文数: {total_placeholders}")
    
    if total_papers > 0:  # type: ignore
        overall_rate = (total_placeholders / total_papers) * 100  # type: ignore
        print(f"📊 总体占位符比例: {overall_rate:.1f}%")
        
        if overall_rate > 50:
            print("🚨 占位符比例过高，建议检查AI提示词配置")
        elif overall_rate > 20:
            print("⚠️ 占位符比例较高，建议优化提示词")
        else:
            print("✅ 占位符比例可接受")
    
    print("\n💡 建议:")
    print("1. 使用context_manager.validate_summary_quality()进行质量检查")
    print("2. 调整AI提示词中的反占位符指令")
    print("3. 启用validator.py进行二次验证")


if __name__ == "__main__":
    main()
