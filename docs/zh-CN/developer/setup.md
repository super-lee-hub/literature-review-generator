# 开发环境搭建

> 受众：贡献者和维护者。
> 本文件面向开发；终端用户请从根目录 README 文件开始。

## 推荐环境

项目主要在 `auto-generate-gui` conda 环境中维护。

```bash
conda env create -f environment.yml
conda activate auto-generate-gui
pip install -r requirements-dev.txt
```

## 依赖拆分

- `requirements.txt`：正常运行所需的运行时依赖
- `requirements-dev.txt`：开发、测试和类型检查依赖
- `environment.yml`：推荐的 conda 环境引导文件

## GUI E2E 测试（可选）

基于 Playwright 的 GUI 测试是可选的。如需在本地运行：

```bash
python -m playwright install chromium
pytest -q tests/test_gui_playwright.py
```

如果 Playwright 未安装，该测试文件会被设计为自动跳过。

## 常用开发命令

运行全部测试：

```bash
pytest -q
```

运行类型检查：

```bash
pyright
```

以开发模式启动 GUI：

```bash
start_gui_dev.bat
```

或：

```bash
python launch_gui.py --reload --no-show
```

## 不要提交的内容

不要提交以下本地或生成的文件：

- `.env`
- `config.ini`
- `output/`
- `logs/`
- `tmp/`
- `venv/`
- IDE 设置或缓存目录

这些已在 `.gitignore` 中覆盖。
