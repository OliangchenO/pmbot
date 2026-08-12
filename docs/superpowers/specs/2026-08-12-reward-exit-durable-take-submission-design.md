# Durable reward-exit take submission

已获用户确认。每个 batch take 在发出请求前持久化为 `PENDING`；回执的 reported fill 与 WebSocket 的真实 fill 分别记录。只有真实累计 fill 达到回执数量时才关闭提交记录并允许补单。请求异常、未知回执和重启均保留 `PENDING`，因此 fail-closed，不会重复 take。
