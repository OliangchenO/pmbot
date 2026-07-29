# Live 成交通知（钉钉）设计

## 目标

在不改变任何交易、下单、撤单、仓位或风控流程的前提下，当 live 模式收到真实成交回报时，通过现有 `pmbot/dingtalk.py` 异步发送钉钉通知。paper 模式不发送通知。

## 范围与边界

- 只处理 `LiveBroker.record_user_fill()` 所记录的真实成交回报；不把 `ORDER_PLACED`、订单接受或模拟成交视为成交。
- 买入、卖出及部分成交均各发送一条通知。
- 通知逻辑必须位于现有成交状态、订单缓存、指标和日志更新之后，并且任何配置或网络异常均不得传播到交易流程。
- 不新增下单、撤单、重试、风控或成交确认流程。

## 设计

在配置的 notifications 段增加 `dingtalk` 设置：`enabled`、`webhook_url` 与可选 `secret`。未启用或未提供 Webhook 时不创建通知器。

`LiveBroker` 接收一个可选的通知器依赖；启动 live broker 的装配点根据配置构造 `DingTalkNotifier` 并注入。`record_user_fill()` 在完成其既有更新后，格式化包含市场、买卖方向、YES/NO、数量和价格的文本，再调用 `send_text()`。

`DingTalkNotifier.send_text()` 仅入队并由守护线程发送，因此不会等待网络。通知调用额外以异常保护包裹：通知失败仅记录 warning，不影响已确认的成交处理。

## 测试

- live broker 收到成交时，向注入的通知器发送一条含成交关键字段的通知。
- paper broker 成交不调用通知器。
- 通知器抛出异常时，live broker 的成交状态和指标更新仍完成。

## 非目标

- 不改变现有交易或成交确认机制。
- 不发送下单、撤单、行情、模拟成交或历史成交通知。
- 不在代码、测试输出或日志中写入 Webhook 与签名密钥。
