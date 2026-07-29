# 交易审计日志设计

## 目标

为 live 模式建立可顺序追加、可机器解析的 JSONL 审计流。任何订单生命周期、WS 成交和合并操作都必须能用同一关联标识还原，并在合并确认后明确说明实际兑回金额或未能取得该金额的原因。

## 事件模型

新增 `AuditLogger`，每行一个 JSON 对象，至少包含 `ts`、`event`、`cid`、`market`。订单事件包含 `order_id`；成交事件包含 `order_id`、`fill_id` 和上游可提供的 `trade_hash`；所有风险相关下单事件还包含 `path`（`normal`、`inventory_recovery`、`cooldown_recovery`、`forced_hedge`）、`unpaired_cost`、`pair_cap`、`expected_pair_pnl`。

路径由策略层在创建订单前标注，订单对象保留该标注，WS 成交按 `order_id` 找回，无法关联时明确写 `path: unknown`，不伪造来源。

## 合并闭环

`Merger` 返回结构化结果而非裸布尔值：提交时记录 `merge_submitted` 和 relayer `transaction_id` 或自提交 `tx_hash`；确认时记录 `merge_confirmed`，带交易哈希和 `redeemed_usd`；失败记录 `merge_failed`。对 relayer 确认响应中没有链上哈希或余额增量时，记录字段为 `null` 和原因，不能把名义 `pairs` 当作实际兑回额。

## 存储与故障处理

审计流由配置项 `metrics.audit_log` 指向，默认 `data/audit.jsonl`。每次追加后 flush；写入失败只记录运行时错误，不中断交易循环。现有人类可读 `pmbot.log` 保留为摘要，不能取代 JSONL。

## 验证

覆盖：带路径和成本上限的下单/撤单记录；WS 成交取到订单、成交与交易标识；未知订单的显式未知路径；relayer 的提交、确认、哈希和兑回；确认响应缺少金额时不虚构兑回。
