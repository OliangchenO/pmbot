# 带敞口市场不占候选名额设计

## 目标

带有可管理未配对敞口的市场必须继续留在报价集合中。敞口低于该市场奖励最低
挂单量时不占用 `scanner.top_n_markets` 的普通候选名额；达到该最低挂单量时占用一个名额。

## 范围

- 沿用现有的敞口判定：`abs(broker.unpaired_shares(m)) >= MIN_TAKER_SHARES`，当前
  阈值为 5 股。
- 每轮扫描先收集这些带敞口市场，无论是否仍在本轮 `ranked` 中都保留。
- `abs(unpaired_shares) < market.min_size` 的带敞口市场为免费保留市场；
  `abs(unpaired_shares) >= market.min_size` 的带敞口市场为占位锁定市场。
- `top_n_markets` 先扣除占位锁定市场的数量，再由普通候选（沿用既有 sticky-swap 规则）填补；
  免费保留市场最后附加到结果中。
- 最终 `self.markets` 最多包含 `top_n_markets + len(free_exposure_markets)` 个市场。
- 当未配对敞口低于阈值后，市场不再获得额外保留资格；已有候选筛选规则决定它是否留下。
- 不改变互补报价、冷却恢复、强制对冲及其他风险路径。

## 设计

`_select_markets()` 接收带敞口市场后，先按 `abs(unpaired_shares)` 与
`market.min_size` 分为占位锁定和免费保留两类。占位锁定按当前行为扣减
`top_n_markets` 容量；免费保留不扣减容量。普通粘性选择完成后，将两类带敞口市场
置于结果前面。以 condition id 去重，避免带敞口市场同时重复出现在普通集合。

## 验证

新增端到端重扫测试：`top_n_markets=2` 时，未配对 NO 仓位为 5、奖励最低挂单量为 10 股
的市场即使掉出排名，结果仍包含该市场以及两个普通候选市场；将其敞口提高到 10 股时，
结果为该市场加一个普通候选市场；仓位归零后的下一次重扫只保留两个普通候选市场。
运行定向测试和完整测试套件，不启动 live 交易。
