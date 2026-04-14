from pathlib import Path


def test_queue_page_copy_has_no_placeholder_question_marks() -> None:
    content = Path("gui/app.py").read_text(encoding="utf-8")
    assert 't("?????????")' not in content
    assert 't("???")' not in content
    assert 't("PDF ???")' not in content
    assert 't("Zotero ????")' not in content


def test_queue_page_copy_uses_expected_labels() -> None:
    content = Path("gui/app.py").read_text(encoding="utf-8")
    assert 't("添加任务到队列")' in content
    assert 't("加入草稿")' in content
    assert 't("立即入队")' in content
    assert 't("提交草稿")' in content
    assert 't("清空草稿")' in content
    assert 't("队列草稿为空。你可以先添加多个任务，再统一提交到队列。")' in content
