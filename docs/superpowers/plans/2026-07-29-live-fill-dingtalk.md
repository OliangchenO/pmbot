# Live Fill DingTalk Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 live 模式收到真实成交回报后，以不阻塞交易流程的方式发送钉钉通知。

**Architecture:** `pmbot.main` 只在 live 模式根据配置创建现有 `DingTalkNotifier`，并作为可选依赖传入 `LiveBroker`。`LiveBroker.record_user_fill()` 保持原有状态更新顺序，全部完成后尝试异步入队通知；通知异常被捕获并记录 warning。

**Tech Stack:** Python 3、PyYAML、pytest、现有 `pmbot.dingtalk.DingTalkNotifier`。

## Global Constraints

- 仅处理 `LiveBroker.record_user_fill()` 的真实成交回报；不得用订单提交或订单接受替代成交。
- paper 模式绝不创建或调用钉钉通知。
- 不改变下单、撤单、仓位、指标、日志、风控或成交确认流程。
- 通知必须异步入队；通知配置或发送失败不得影响成交状态更新或抛出到交易循环。
- Webhook 和签名密钥不得写入代码、测试断言或日志。

---

### Task 1: 为 live 成交增加可选通知依赖与回归测试

**Files:**
- Modify: `tests/test_brokers.py:620-668`
- Modify: `pmbot/brokers.py:548-612,1031-1083`

**Interfaces:**
- Consumes: `DingTalkNotifier.send_text(content: str) -> bool`。
- Produces: `LiveBroker(cfg: dict, tracker: BookTracker, notifier: object | None = None)`；成交后调用 `notifier.send_text(content)`。

- [ ] **Step 1: 写入会失败的成交通知测试**

```python
def test_live_fill_notifies_after_recording_fill():
    stub = _live_fill_stub()
    stub.notifier = MagicMock()
    market = stub._markets["cid1"]

    LiveBroker.record_user_fill(stub, market.yes_token, "BUY", 0.47, 3.0)

    message = stub.notifier.send_text.call_args.args[0]
    assert market.question in message
    assert "BUY" in message and "YES" in message
    assert "3" in message and "0.470" in message


def test_live_fill_survives_notification_error(caplog):
    stub = _live_fill_stub()
    stub.notifier = MagicMock()
    stub.notifier.send_text.side_effect = RuntimeError("network unavailable")
    market = stub._markets["cid1"]

    LiveBroker.record_user_fill(stub, market.yes_token, "BUY", 0.47, 3.0)

    assert stub.fills_log[-1]["size"] == 3.0
    assert "DingTalk fill notification failed" in caplog.text
```

- [ ] **Step 2: 运行测试，确认当前实现失败**

Run: `python -m pytest tests/test_brokers.py -k "live_fill_notifies or live_fill_survives_notification_error" -v`

Expected: FAIL，因为当前 `record_user_fill()` 没有读取或调用 `stub.notifier`。

- [ ] **Step 3: 实现最小的可选通知路径**

```python
def __init__(self, cfg: dict, tracker: BookTracker, notifier=None):
    # 保留全部现有初始化逻辑
    self.notifier = notifier

def _notify_fill(self, entry: dict, order_side: str) -> None:
    if self.notifier is None:
        return
    try:
        self.notifier.send_text(
            "[PMBot LIVE FILL] "
            f"{entry['market']} | {order_side} {entry['side']} "
            f"{entry['size']:.2f} @ {entry['price']:.3f}"
        )
    except Exception as exc:
        log.warning("DingTalk fill notification failed: %s", exc)
```

在 `record_user_fill()` 的既有 `fills_log`、metrics 与 `LIVE FILL` 日志更新完成之后调用 `self._notify_fill(entry, side)`。用 `getattr(self, "notifier", None)` 兼容现有测试 stub。

- [ ] **Step 4: 运行定向测试，确认通过**

Run: `python -m pytest tests/test_brokers.py -k "live_fill_notifies or live_fill_survives_notification_error or pending_hedge_survives" -v`

Expected: PASS；异常测试证明通知失败不会中断成交更新。

- [ ] **Step 5: 提交此任务**

```bash
git add pmbot/brokers.py tests/test_brokers.py
git commit -m "feat: notify DingTalk on live fills"
```

### Task 2: 在 live 装配点从配置注入通知器

**Files:**
- Modify: `pmbot/main.py:440-450,724-740`
- Modify: `config.yaml:after mode`
- Modify: `.env.example:after POLYMARKET_FUNDER`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `LiveBroker(cfg, tracker, notifier=None)` 与 `DingTalkNotifier(webhook_url: str, secret: str | None = None)`。
- Produces: `_build_live_notifier(cfg: dict) -> DingTalkNotifier | None`。

- [ ] **Step 1: 写入会失败的装配测试**

```python
def test_build_live_notifier_uses_enabled_dingtalk_settings(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://example.test/robot")
    monkeypatch.setenv("DINGTALK_SECRET", "SEC-test")
    cfg = {"notifications": {"dingtalk": {"enabled": True}}}

    notifier = main._build_live_notifier(cfg)

    assert notifier.webhook_url == "https://example.test/robot"
    assert notifier.secret == "SEC-test"


def test_build_live_notifier_is_disabled_without_explicit_enable(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://example.test/robot")
    assert main._build_live_notifier({}) is None
```

- [ ] **Step 2: 运行测试，确认当前实现失败**

Run: `python -m pytest tests/test_main.py -k "build_live_notifier" -v`

Expected: FAIL with `AttributeError: module 'pmbot.main' has no attribute '_build_live_notifier'`。

- [ ] **Step 3: 实现配置读取与 live 注入**

```python
def _build_live_notifier(cfg: dict):
    settings = (cfg.get("notifications") or {}).get("dingtalk") or {}
    if not settings.get("enabled", False):
        return None
    webhook_url = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.warning("DingTalk is enabled but DINGTALK_WEBHOOK_URL is unset")
        return None
    from .dingtalk import DingTalkNotifier
    return DingTalkNotifier(webhook_url, os.environ.get("DINGTALK_SECRET") or None)
```

将两处 `LiveBroker(self.cfg, self.tracker)` 替换为 `LiveBroker(self.cfg, self.tracker, _build_live_notifier(self.cfg))`；不要修改任何 `PaperBroker(...)` 调用。在 `config.yaml` 加入：

```yaml
notifications:
  dingtalk:
    enabled: false
```

在 `.env.example` 加入空白的 `DINGTALK_WEBHOOK_URL=` 和 `DINGTALK_SECRET=`，并说明只在 live 通知显式启用时读取。

- [ ] **Step 4: 运行装配测试，确认通过**

Run: `python -m pytest tests/test_main.py -k "build_live_notifier" -v`

Expected: PASS；禁用配置时不创建通知器。

- [ ] **Step 5: 提交此任务**

```bash
git add pmbot/main.py config.yaml .env.example tests/test_main.py
git commit -m "feat: configure live fill DingTalk alerts"
```

### Task 3: 完整验证与配置安全检查

**Files:**
- Verify: `pmbot/brokers.py`, `pmbot/main.py`, `config.yaml`, `.env.example`, `tests/test_brokers.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: Tasks 1–2 的通知依赖和配置装配。
- Produces: 已验证的“仅 live 真实成交通知、paper 不通知、通知失败不影响成交”行为。

- [ ] **Step 1: 运行 broker 测试文件**

Run: `python -m pytest tests/test_brokers.py -v`

Expected: PASS；覆盖部分成交、taker 成交和既有订单状态逻辑。

- [ ] **Step 2: 运行 main 测试文件**

Run: `python -m pytest tests/test_main.py -v`

Expected: PASS；确认 live 初始化和 paper 初始化均未回归。

- [ ] **Step 3: 运行完整测试集**

Run: `python -m pytest -q`

Expected: PASS；若出现现有失败，记录测试名和失败原因，不将其归因于本改动。

- [ ] **Step 4: 检查敏感配置与改动范围**

Run: `git diff --check && git diff -- config.yaml .env.example pmbot/main.py pmbot/brokers.py tests/test_brokers.py tests/test_main.py`

Expected: 无空白错误；只出现开关和空白环境变量名，绝不出现真实 Webhook 或签名密钥。

- [ ] **Step 5: 提交验证后的最终状态**

```bash
git status --short
git add pmbot/brokers.py pmbot/main.py config.yaml .env.example tests/test_brokers.py tests/test_main.py
git commit -m "test: verify live fill DingTalk notifications"
```

