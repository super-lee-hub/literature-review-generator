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


def test_old_queue_builder_copy_is_absent_from_normal_ui() -> None:
    app_content = Path("gui/app.py").read_text(encoding="utf-8")
    i18n_content = Path("gui/i18n.py").read_text(encoding="utf-8")

    for old_copy in [
        "加入草稿",
        "立即入队",
        "提交草稿",
        "清空草稿",
        "队列草稿",
        "队列文件操作",
        "队列文件路径",
        "保存队列",
        "加载队列",
        "添加任务到队列",
        "queue draft",
        "Save Queue",
        "Load Queue",
    ]:
        assert old_copy not in app_content
        assert old_copy not in i18n_content


def test_workflow_queue_panel_copy_is_present() -> None:
    content = Path("gui/app.py").read_text(encoding="utf-8")

    for expected_copy in [
        "后台队列",
        "工作台主流程按钮的后台执行区",
        "点击主流程操作按钮",
        "表单不会被锁死",
        "启动后台处理",
        "当前后台任务",
        "队列任务列表",
        "暂无队列任务",
        "后台队列怎么用",
    ]:
        assert expected_copy in content


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
