from pathlib import Path


def test_queue_page_copy_has_no_placeholder_question_marks() -> None:
    app_content = Path("gui/app.py").read_text(encoding="utf-8")
    i18n_content = Path("gui/i18n.py").read_text(encoding="utf-8")

    assert 't("?????????")' not in app_content
    assert 't("???")' not in app_content
    assert 't("PDF ???")' not in app_content
    assert 't("Zotero ????")' not in app_content

    assert '"Task input": "????"' not in i18n_content
    assert '"Project name": "???"' not in i18n_content
    assert '"PDF folder": "PDF ???"' not in i18n_content
    assert '"Select Zotero report file": "?? Zotero ????"' not in i18n_content
    assert '"Please enter a project name first.": "????????"' not in i18n_content


def test_queue_page_copy_uses_expected_labels() -> None:
    content = Path("gui/app.py").read_text(encoding="utf-8")
    assert 't("添加任务到队列")' in content
    assert 't("输入来源")' in content
    assert 't("加入草稿")' in content
    assert 't("立即入队")' in content
    assert 't("提交草稿")' in content
    assert 't("清空草稿")' in content
    assert 't("队列草稿为空。你可以先添加多个任务，再统一提交到队列。")' in content
    assert 't("队列页默认提交标准任务；如果要先做概念增强或自由模式规划，建议先在工作台确认后再入队。")' in content


def test_gui_copy_uses_current_reuse_and_queue_order_labels() -> None:
    app_content = Path("gui/app.py").read_text(encoding="utf-8")
    i18n_content = Path("gui/i18n.py").read_text(encoding="utf-8")

    assert "Auto reuse historical stage-1 summaries" in app_content
    assert "When enabled, stage 1 scans all historical project outputs plus compatible legacy summaries" in app_content
    assert "Enable DOI-only stage-1 reuse" not in app_content
    assert "上移选中任务" not in app_content
    assert "下移选中任务" not in app_content

    assert '"Task input": "任务输入"' in i18n_content
    assert '"Project name": "项目名"' in i18n_content
    assert '"PDF folder": "PDF 文件夹"' in i18n_content
    assert '"Open PDF folder": "打开 PDF 文件夹"' in i18n_content
    assert '"Please enter a project name first.": "请先填写项目名。"' in i18n_content
    assert '"Enable DOI-only stage-1 reuse"' not in i18n_content
