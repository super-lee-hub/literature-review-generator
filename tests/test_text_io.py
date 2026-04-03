from pathlib import Path

from services.text_io import load_json_file_with_fallbacks, read_text_file_with_fallbacks


def test_read_text_file_with_fallbacks_preserves_utf8_chinese(tmp_path: Path) -> None:
    sample = "令牌桶状态: 正常\nAI返回非字典格式，尝试手动解析"
    target = tmp_path / "utf8.txt"
    target.write_bytes(sample.encode("utf-8"))

    assert read_text_file_with_fallbacks(target) == sample


def test_read_text_file_with_fallbacks_reads_gb18030_chinese(tmp_path: Path) -> None:
    sample = "中文标题\n作者\t张三"
    target = tmp_path / "gbk.txt"
    target.write_bytes(sample.encode("gb18030"))

    assert read_text_file_with_fallbacks(target) == sample


def test_load_json_file_with_fallbacks_preserves_gb18030_chinese(tmp_path: Path) -> None:
    target = tmp_path / "summary.json"
    target.write_bytes('{"title":"中文标题","journal":"研究期刊"}'.encode("gb18030"))

    payload = load_json_file_with_fallbacks(target)

    assert payload["title"] == "中文标题"
    assert payload["journal"] == "研究期刊"
