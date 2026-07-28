# 运行日志本地持久化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将每次运行的控制台日志同步持久化到本地、按日轮转且永久保留。

**Architecture:** 在 `pmbot.main` 中抽出 `configure_logging()`，统一构造现有 Rich 控制台 handler 和新的 UTF-8 文件 handler。文件 handler 写入 `logs/pmbot.log`，由标准库在本地零点轮转；测试用临时工作目录隔离真实日志文件。

**Tech Stack:** Python 标准库 `logging`、`logging.handlers.TimedRotatingFileHandler`、Rich、pytest。

## Global Constraints

- 保留现有 Rich 控制台输出与 INFO 日志级别。
- 文件路径固定为相对当前工作目录的 `logs/pmbot.log`。
- 每日零点轮转，`backupCount=0`；不得压缩、清理或删除历史日志。
- 文件编码为 UTF-8；日志行须包含时间、级别、logger 名称与消息。
- `logs/` 不得进入 Git。

---

### Task 1: 增加可测试的日志配置

**Files:**
- Modify: `pmbot/main.py:1-35,965-994`
- Modify: `tests/test_main.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `configure_logging(log_dir: pathlib.Path | str = "logs") -> logging.Logger`，配置根日志器并返回它。
- Consumes: `rich.logging.RichHandler`、`logging.handlers.TimedRotatingFileHandler`。

- [ ] **Step 1: 写入失败测试**

```python
def test_configure_logging_writes_utf8_daily_rotating_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = main.configure_logging()
    root.info("持久化测试消息")
    for handler in root.handlers:
        handler.flush()

    log_file = tmp_path / "logs" / "pmbot.log"
    assert "持久化测试消息" in log_file.read_text(encoding="utf-8")
    file_handler = next(
        h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)
    )
    assert file_handler.when == "MIDNIGHT"
    assert file_handler.backupCount == 0
```

- [ ] **Step 2: 运行测试，确认因缺少函数而失败**

Run: `pytest tests/test_main.py::test_configure_logging_writes_utf8_daily_rotating_file -v`

Expected: FAIL，提示 `pmbot.main` 没有 `configure_logging`。

- [ ] **Step 3: 实现最小日志配置**

```python
def configure_logging(log_dir: Path | str = "logs") -> logging.Logger:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        directory / "pmbot.log", when="midnight", backupCount=0,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(
        level=logging.INFO, format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False), file_handler],
        force=True,
    )
    return logging.getLogger()
```

将 `main()` 中现有 `logging.basicConfig(...)` 替换为 `configure_logging()`；保留
`logging.getLogger("httpx").setLevel(logging.WARNING)`。在 `.gitignore` 新增独立
一行 `logs/`，使运行日志及其轮转文件不进入 Git。

- [ ] **Step 4: 运行聚焦测试，确认通过**

Run: `pytest tests/test_main.py::test_configure_logging_writes_utf8_daily_rotating_file -v`

Expected: PASS，并在 pytest 临时目录生成 UTF-8 日志文件。

- [ ] **Step 5: 运行完整测试集**

Run: `pytest tests/ -v`

Expected: 所有既有测试与新增测试通过。

- [ ] **Step 6: 检查 Git 忽略与差异范围**

Run: `git check-ignore logs/pmbot.log && git diff --check && git status --short`

Expected: `logs/pmbot.log` 被忽略；无空白错误；不修改用户既有的 `README.md`、`config.yaml` 和 `.idea/` 变更。
