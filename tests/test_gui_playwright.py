from __future__ import annotations

import configparser
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect
sync_playwright = playwright.sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_gui_test_config(config_path: Path, output_dir: Path) -> None:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "config.ini.example", encoding="utf-8")
    parser.setdefault("Paths", {})
    parser["Paths"]["output_path"] = str(output_dir)
    parser["Paths"]["zotero_report"] = ""
    parser["Paths"]["library_path"] = ""
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _wait_for_server(base_url: str, process: subprocess.Popen[str], timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base_url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)

    output = ""
    if process.stdout:
        try:
            output = process.stdout.read()
        except Exception:
            output = ""
    raise RuntimeError(f"GUI server did not start in time.\nCaptured output:\n{output}")


def _notification(page):
    return page.locator(".q-notification").last


def _field_input(page, label_text: str):
    return page.locator(".q-field", has_text=label_text).locator("input,textarea").first


def _editable_field_input(page, label_text: str):
    return (
        page.locator(".q-field", has_text=label_text)
        .locator("input:not([readonly]),textarea:not([readonly])")
        .last
    )


def _set_path_value(page, *, open_button_name: str, label_text: str, value: str) -> None:
    page.get_by_role("button", name=open_button_name).click()
    editable_input = _editable_field_input(page, label_text)
    expect(editable_input).to_be_visible()
    editable_input.fill(value)
    page.get_by_role("button", name="保存路径设置").click()
    expect(_field_input(page, label_text)).to_have_value(value)


def _open_page(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator(".ag-fixedbar-shell")).to_be_visible()


def _card(page, title_text: str):
    return page.locator(".ag-card", has_text=title_text).first


@pytest.fixture()
def gui_server(tmp_path):
    temp_dir = tmp_path / "gui_playwright"
    temp_dir.mkdir()
    output_dir = temp_dir / "output"
    output_dir.mkdir()
    pdf_dir = temp_dir / "pdfs"
    pdf_dir.mkdir()

    config_path = temp_dir / "gui_test_config.ini"
    env_path = temp_dir / "gui_test.env"
    env_path.write_text("", encoding="utf-8")
    _write_gui_test_config(config_path, output_dir)

    port = _pick_free_port()
    env = os.environ.copy()
    env["AUTO_GENERATE_GUI_TEST_MODE"] = "1"
    env["AUTO_GENERATE_ENV_PATH"] = str(env_path)
    env["NICEGUI_SCREEN_TEST_PORT"] = str(port)

    process = subprocess.Popen(
        [sys.executable, "launch_gui.py", "--no-show", "--port", str(port), "--config", str(config_path)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    _wait_for_server(base_url, process)

    yield {
        "base_url": base_url,
        "config_path": config_path,
        "env_path": env_path,
        "output_dir": output_dir,
        "pdf_dir": pdf_dir,
    }

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 1100})
    page = context.new_page()
    yield page
    page.close()
    context.close()


def test_dashboard_shows_search_topbar(page, gui_server):
    _open_page(page, gui_server["base_url"])

    expect(page.locator(".ag-search")).to_be_visible()
    expect(page.locator(".ag-topbar-title")).to_contain_text("auto-generate")
    expect(page.locator(".ag-reminder-text")).to_contain_text("工作台已就绪")


def test_dashboard_reminder_is_below_topbar_and_extra_sections_render(page, gui_server):
    _open_page(page, gui_server["base_url"])

    fixedbar_box = page.locator(".ag-fixedbar").bounding_box()
    reminder_box = page.locator(".ag-page-reminder").bounding_box()
    assert fixedbar_box is not None
    assert reminder_box is not None
    assert reminder_box["y"] >= fixedbar_box["y"] + fixedbar_box["height"] - 1

    expect(page.get_by_text("现在建议做什么", exact=True)).to_be_visible()
    expect(page.get_by_text("当前工作台快照", exact=True)).to_be_visible()
    assert page.locator(".ag-page .ag-card").count() >= 4


@pytest.mark.parametrize(
    ("query", "route_suffix"),
    [
        ("OCR", "/setup/processing"),
        ("API", "/setup/api"),
        ("日志", "/logs"),
        ("自由模式", "/workflow"),
        ("帮助", "/guide"),
    ],
)
def test_search_routes_cover_major_pages(page, gui_server, query, route_suffix):
    _open_page(page, gui_server["base_url"])
    page.locator(".ag-fixedbar .ag-search input").last.fill(query)
    page.locator(".ag-fixedbar .ag-search-button").last.click()
    expect(page).to_have_url(re.compile(re.escape(route_suffix) + r"$"))


def test_topbar_buttons_and_dashboard_navigation(page, gui_server):
    _open_page(page, gui_server["base_url"])

    page.get_by_role("button", name="进入工作台").first.click()
    expect(page).to_have_url(re.compile(r"/workflow$"))

    _open_page(page, gui_server["base_url"])
    page.get_by_role("button", name="前往设置").first.click()
    expect(page).to_have_url(re.compile(r"/setup$"))


def test_sidebar_navigation_links(page, gui_server):
    _open_page(page, gui_server["base_url"])

    nav_targets = [
        ("工作台", r"/workflow$"),
        ("环境与路径", r"/setup$"),
        ("API 与模型", r"/setup/api$"),
        ("性能与预处理", r"/setup/processing$"),
        ("结果与日志", r"/logs$"),
        ("使用引导", r"/guide$"),
        ("队列", r"/queue$"),
    ]
    for label, pattern in nav_targets:
        page.locator(".ag-nav-link", has_text=label).first.click(no_wait_after=True)
        expect(page).to_have_url(re.compile(pattern))


def test_setup_page_can_save_temp_config(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/setup')

    new_output_dir = gui_server["output_dir"] / "changed_output"
    new_output_dir.mkdir(exist_ok=True)
    zotero_report = gui_server["output_dir"] / "zotero_report.md"
    zotero_report.write_text("# zotero export", encoding="utf-8")

    _set_path_value(page, open_button_name="选择输出目录", label_text="输出目录", value=str(new_output_dir))
    _set_path_value(page, open_button_name="选择 Zotero 报告文件", label_text="Zotero 报告", value=str(zotero_report))

    page.get_by_role("button", name="保存配置").nth(1).click()
    expect(_notification(page)).to_contain_text("配置已保存")

    parser = configparser.ConfigParser()
    parser.read(gui_server["config_path"], encoding="utf-8")
    assert parser["Paths"]["output_path"] == str(new_output_dir)
    assert parser["Paths"]["zotero_report"] == str(zotero_report)

    page.get_by_role("button", name="打开输出目录").click()
    expect(_notification(page)).to_contain_text("测试模式：已模拟打开路径")


def test_processing_page_can_persist_settings(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/setup/processing')

    _editable_field_input(page, "最大并发").fill("7")
    _editable_field_input(page, "API 重试次数").fill("9")
    _set_path_value(
        page,
        open_button_name="选择缓存目录",
        label_text="缓存目录",
        value=str(gui_server["output_dir"] / "cache_area"),
    )
    _editable_field_input(page, "OCR 语言").fill("eng+chi_sim")

    for label in ["启用阶段二自动重试", "启用预处理", "强制重建缓存", "启用本地 RAG"]:
        page.locator(".q-toggle", has_text=label).click()

    page.get_by_role("button", name="保存配置").first.click()
    expect(_notification(page)).to_contain_text("配置已保存")

    parser = configparser.ConfigParser()
    parser.read(gui_server["config_path"], encoding="utf-8")
    assert parser["Performance"]["max_workers"] == "7"
    assert parser["Performance"]["api_retry_attempts"] == "9"
    assert parser["Preprocess"]["cache_dir"].endswith("cache_area")
    assert parser["Preprocess"]["ocr_languages"] == "eng+chi_sim"


def test_first_api_card_buttons_work(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/setup/api')

    reader_card = _card(page, "阅读模型")
    api_base_input = reader_card.locator(".q-field", has_text="API Base").locator("input").first
    api_base_input.fill("https://api.openai.com/v1/chat/completions")
    reader_card.get_by_role("button", name="检查配置").click()
    expect(reader_card.locator(".ag-inline-alert")).to_contain_text("接口路径")

    reader_card.get_by_role("button", name="规范化 URL").click()
    expect(api_base_input).not_to_have_value(re.compile(r".*/chat/completions/?$"))
    expect(reader_card.locator(".ag-inline-alert")).to_contain_text(re.compile(r"模型名|API Key"))

    reader_card.get_by_role("button", name="套用预设 URL").click()
    expect(api_base_input).not_to_have_value("")

    reader_card.get_by_role("button", name="测试连接").click()
    expect(_notification(page)).to_contain_text("测试模式：已模拟 API 连通性检查")
    expect(reader_card.locator(".ag-inline-alert")).to_contain_text("测试模式：已模拟 API 连通性检查")


def test_all_api_cards_expose_actions(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/setup/api')

    for title in ["阅读模型", "备用阅读模型", "写作模型", "大纲模型", "自由模式对话模型", "验证模型"]:
        card = _card(page, title)
        expect(card).to_be_visible()
        expect(card.get_by_role("button", name="套用预设 URL")).to_be_visible()
        expect(card.get_by_role("button", name="规范化 URL")).to_be_visible()
        expect(card.get_by_role("button", name="检查配置")).to_be_visible()
        expect(card.get_by_role("button", name="测试连接")).to_be_visible()
        card.get_by_role("button", name="测试连接").click()
        expect(page.locator(".ag-reminder-text")).to_contain_text("测试模式：已模拟 API 连通性检查")

    mineru_card = _card(page, "MinerU 远程解析")
    expect(mineru_card).to_be_visible()
    expect(mineru_card.locator(".q-field", has_text="Base URL")).to_be_visible()
    expect(mineru_card.locator(".q-field", has_text="API Token")).to_be_visible()
    expect(mineru_card.get_by_role("button", name="前往性能与预处理")).to_be_visible()


def test_mineru_api_card_can_persist_env_values(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/setup/api')

    mineru_card = _card(page, "MinerU 远程解析")
    mineru_card.locator(".q-field", has_text="Base URL").locator("input").first.fill("https://mineru.example/api/v4")
    mineru_card.locator(".q-field", has_text="API Token").locator("input").first.fill("token-123")
    mineru_card.locator(".q-field", has_text="模型版本").locator("input").first.fill("vlm-pro")

    page.get_by_role("button", name="保存配置").first.click()
    expect(_notification(page)).to_contain_text("配置已保存")

    env_content = gui_server["env_path"].read_text(encoding="utf-8")
    assert "MINERU_BASE_URL=https://mineru.example/api/v4" in env_content
    assert "MINERU_API_TOKEN=token-123" in env_content
    assert "MINERU_MODEL_VERSION=vlm-pro" in env_content

    mineru_card.get_by_role("button", name="前往性能与预处理").click()
    expect(page).to_have_url(re.compile(r"/setup/processing$"))


def test_workflow_validation_warnings(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/workflow')

    page.get_by_role("button", name="仅分析文献").click()
    expect(_notification(page)).to_contain_text("请先填写项目名")

    _field_input(page, "项目名").fill("Need PDFs")
    page.get_by_role("button", name="一键运行").click()
    expect(_notification(page)).to_contain_text("PDF 文件夹模式")


def test_workflow_page_groups_actions_and_idle_progress_is_static(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/workflow')

    for title in ["任务起点", "运行方式", "主流程操作", "第一次运行建议", "相关入口"]:
        expect(page.get_by_text(title, exact=True)).to_be_visible()

    progress_card = _card(page, "任务进度")
    expect(progress_card.locator(".q-linear-progress--indeterminate")).to_have_count(0)

    _editable_field_input(page, "项目名").fill("Progress Smoke Test")
    _set_path_value(page, open_button_name="选择 PDF 文件夹", label_text="PDF 文件夹", value=str(gui_server["pdf_dir"]))
    page.get_by_role("button", name="仅分析文献").click()
    expect(_notification(page)).to_contain_text("测试模式：已模拟执行")
    expect(progress_card.locator(".q-linear-progress--indeterminate")).to_have_count(0)


def test_switching_between_workflow_and_logs_keeps_ui_responsive(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/workflow')
    project_input = _editable_field_input(page, "项目名")
    project_input.fill("Timer Resilience")
    page.wait_for_timeout(1200)

    _open_page(page, f'{gui_server["base_url"]}/logs')
    expect(page.locator("textarea")).to_be_visible()
    page.wait_for_timeout(1200)

    _open_page(page, f'{gui_server["base_url"]}/workflow')
    expect(page.get_by_role("button", name="仅分析文献")).to_be_visible()
    project_input = _editable_field_input(page, "项目名")
    project_input.fill("Still Responsive")
    expect(project_input).to_have_value("Still Responsive")


def test_workflow_free_mode_layout_stays_readable(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/workflow')

    work_mode_toggle = page.locator(".ag-mode-toggle").nth(1)
    work_mode_toggle.locator(".q-btn").nth(2).click()

    toggle_box = work_mode_toggle.bounding_box()
    assert toggle_box is not None
    button_boxes = [work_mode_toggle.locator(".q-btn").nth(index).bounding_box() for index in range(3)]
    assert all(box is not None for box in button_boxes)
    button_boxes = [box for box in button_boxes if box is not None]
    assert min(box["width"] for box in button_boxes) >= (toggle_box["width"] / 3) - 20
    assert button_boxes[0]["x"] - toggle_box["x"] <= 12
    assert (toggle_box["x"] + toggle_box["width"]) - (button_boxes[-1]["x"] + button_boxes[-1]["width"]) <= 12

    planner_outputs = page.locator(".ag-planner-output")
    expect(planner_outputs).to_have_count(2)
    for index in range(2):
        field = planner_outputs.nth(index)
        expect(field).to_be_visible()
        field_box = field.bounding_box()
        assert field_box is not None
        assert field_box["height"] >= 160
        metrics = field.locator("textarea").evaluate(
            "el => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight})"
        )
        assert metrics["clientHeight"] >= 120


def test_workflow_actions_and_links(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/workflow')

    _set_path_value(page, open_button_name="选择 PDF 文件夹", label_text="PDF 文件夹", value=str(gui_server["pdf_dir"]))
    _editable_field_input(page, "项目名").fill("GUI Smoke Test")
    page.get_by_role("button", name="自由模式").click()
    _editable_field_input(page, "继续告诉规划助手").fill("请围绕 research gap 和变量链路组织一个测试大纲。")

    page.get_by_role("button", name="打开 PDF 文件夹").click()
    expect(_notification(page)).to_contain_text("测试模式：已模拟打开路径")

    page.get_by_role("button", name="发送给规划助手").click()
    expect(_field_input(page, "对话记录")).to_have_value(re.compile(r"请围绕 research gap"))

    page.get_by_role("button", name="仅分析文献").click()
    expect(_notification(page)).to_contain_text("自由模式对话还没有应用到本次任务")

    page.get_by_role("button", name="应用到本次任务").click()
    expect(_notification(page)).to_contain_text("自由模式已应用到本次任务")

    for button_name in ["仅分析文献", "生成大纲", "生成全文", "一键运行"]:
        page.get_by_role("button", name=button_name).click()
        expect(_notification(page)).to_contain_text("测试模式：已模拟执行")

    page.get_by_role("button", name="补跑、恢复与验证（按需展开）").click()
    for button_name in ["验证综述", "重试失败论文"]:
        page.get_by_role("button", name=button_name).click()
        expect(_notification(page)).to_contain_text("测试模式：已模拟执行")

    _card(page, "相关入口").get_by_role("button", name="前往设置").click()
    expect(page).to_have_url(re.compile(r"/setup$"))


def test_logs_and_guide_pages_render(page, gui_server):
    _open_page(page, f'{gui_server["base_url"]}/logs')
    expect(page.locator("textarea")).to_be_visible()

    page.get_by_role("button", name="打开日志目录").click()
    expect(_notification(page)).to_contain_text("测试模式：已模拟打开路径")

    page.get_by_role("button", name="打开输出目录").click()
    expect(_notification(page)).to_contain_text("测试模式：已模拟打开路径")

    _open_page(page, f'{gui_server["base_url"]}/guide')
    expect(page.locator(".ag-card")).to_have_count(4)
    expect(page.get_by_text("第一次运行，只看这一页也能开始", exact=True)).to_be_visible()
    expect(page.get_by_text("输入方式说明", exact=True)).to_be_visible()
    expect(page.get_by_text("运行方式说明", exact=True)).to_be_visible()
    expect(page.get_by_text("关于 OCR、MinerU、复用和工作区", exact=True)).to_be_visible()


def test_language_switch_changes_labels(page, gui_server):
    _open_page(page, gui_server["base_url"])

    page.locator(".q-select").first.click()
    page.get_by_text("English", exact=True).click()
    page.wait_for_load_state("networkidle")
    expect(page.get_by_role("button", name="Search")).to_be_visible()
    expect(page.locator(".ag-topbar-title")).to_contain_text("auto-generate")
