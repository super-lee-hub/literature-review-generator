#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Excel数据提取逻辑测试脚本 - 修正版
验证修复后的数据提取是否能正确从JSON中获取信息
"""

import json
import sys
import os

def test_data_extraction_fixed():
    """测试修复后的数据提取逻辑"""
    try:
        # 读取JSON文件
        json_file = "output/案例分析/案例分析_summaries.json"
        
        if not os.path.exists(json_file):
            print(f"❌ JSON文件不存在: {json_file}")
            return False
        
        with open(json_file, 'r', encoding='utf-8') as f:
            summaries = json.load(f)
        
        print(f"✅ 成功加载JSON文件，共 {len(summaries)} 篇论文")
        print("=" * 80)
        
        # 测试前3篇论文的数据提取
        for i, summary in enumerate(summaries[:3]):
            print(f"\n📄 测试论文 #{i+1}:")
            
            # 检查数据源（修正后的路径）
            has_paper_info = 'paper_info' in summary
            has_ai_summary = 'ai_summary' in summary
            has_common_core = has_ai_summary and 'common_core' in summary['ai_summary']
            
            print(f"  📊 数据源检查:")
            print(f"    - paper_info: {'✅' if has_paper_info else '❌'}")
            print(f"    - ai_summary: {'✅' if has_ai_summary else '❌'}")
            print(f"    - ai_summary.common_core: {'✅' if has_common_core else '❌'}")
            
            if not has_common_core:
                print(f"  ⚠️  跳过：没有ai_summary.common_core数据")
                continue
            
            # 应用正确的路径
            paper_info = summary.get('paper_info', {})
            common_core = summary['ai_summary']['common_core']
            
            # 论文基本信息（优先从paper_info提取，备选从common_core提取）
            title = paper_info.get('title', '') or common_core.get('title', '')
            authors = ', '.join(paper_info.get('authors', [])) if paper_info.get('authors') else (', '.join(common_core.get('authors', [])) if common_core.get('authors') else '')
            year = paper_info.get('year', '') or common_core.get('year', '')
            journal = paper_info.get('journal', '') or common_core.get('journal', '')
            
            # 核心分析内容
            summary_text = common_core.get('summary', '')
            methodology = common_core.get('methodology', '')
            findings = common_core.get('findings', '')
            conclusions = common_core.get('conclusions', '')
            relevance = common_core.get('relevance', '')
            limitations = common_core.get('limitations', '')
            
            print(f"  📋 提取结果:")
            print(f"    标题: {title[:50]}{'...' if len(title) > 50 else ''}")
            print(f"    作者: {authors}")
            print(f"    年份: {year}")
            print(f"    期刊: {journal}")
            print(f"    摘要长度: {len(summary_text)} 字符")
            print(f"    方法长度: {len(methodology)} 字符")
            print(f"    发现长度: {len(findings)} 字符")
            print(f"    结论长度: {len(conclusions)} 字符")
            
            # 检查数据完整性
            basic_fields = [title, authors, year, journal]
            analysis_fields = [summary_text, methodology, findings, conclusions]
            
            basic_empty = sum(1 for field in basic_fields if not field.strip())
            analysis_empty = sum(1 for field in analysis_fields if not field.strip())
            
            print(f"  📈 数据完整性:")
            print(f"    基本信息: {4-basic_empty}/4 字段有数据")
            print(f"    分析内容: {4-analysis_empty}/4 字段有数据")
            
            if basic_empty == 0 and analysis_empty == 0:
                print(f"  ✅ 数据提取成功 - 所有字段都有内容")
            elif basic_empty < 4:
                print(f"  ⚠️  部分数据缺失 - 基本信息不完整")
            else:
                print(f"  ❌ 数据提取失败 - 基本信息完全缺失")
        
        print("\n" + "=" * 80)
        print("🔍 数据提取测试完成")
        return True
        
    except Exception as e:
        print(f"❌ 测试过程中出错: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """主函数"""
    print("🧪 开始Excel数据提取逻辑测试（修正版）...")
    print("测试修复后的数据提取是否能正确从JSON中获取信息")
    
    success = test_data_extraction_fixed()
    
    if success:
        print("\n🎉 测试完成！如果所有字段都有数据，说明修复成功")
        print("💡 建议：重新运行程序生成Excel文件以应用修复")
    else:
        print("\n❌ 测试失败，需要进一步调试")

if __name__ == "__main__":
    main()