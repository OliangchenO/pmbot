# Reward Exit Manual Hold Implementation Plan

**Goal:** 补齐奖励退出互补仓后把市场安全交给人工，并在仓位清空后自动恢复。

1. 为“补齐后撤单并转 MANUAL_HOLD”写失败生命周期测试。
2. 为“最大仓位低于 5 才恢复”写失败生命周期测试。
3. 增加按 CID 撤销所有机器人订单的 broker 接口，并在批次密封时调用。
4. 在奖励退出 tick 中关闭已清仓的 `MANUAL_HOLD` 批次并记录市场名称。
5. 运行奖励退出、broker、编译和补丁检查。
