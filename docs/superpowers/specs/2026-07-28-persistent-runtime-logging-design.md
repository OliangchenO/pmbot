# 运行日志本地持久化

## 目标

将机器人运行日志写入本地文件，使控制台关闭后仍可复盘 live 和 paper
会话；不改变任何交易逻辑。

## 设计

`pmbot.main.configure_logging()` 配置根日志器的两个 handler：

1. 保留现有 Rich 控制台 handler。
2. 新增 UTF-8 `TimedRotatingFileHandler`，写入 `logs/pmbot.log`。

文件 handler 在本地时间每日零点轮转，并设置 `backupCount=0`，因此永久
保留每个历史日的日志文件。目录不存在时自动创建 `logs/`；`logs/` 继续被
Git 忽略。文件日志记录时间、级别、logger 名称和消息，方便脱离 Rich 控制台
布局进行成交、告警与异常复盘。

## 异常处理

如无法创建日志目录或打开日志文件，启动应显式失败，不能悄悄失去审计记录。
文件创建成功时，原有控制台日志输出保持不变。

## 验证

新增一个聚焦测试：在临时目录配置日志、写入一条记录，并验证
`logs/pmbot.log` 已包含该记录，且 handler 使用每日零点轮转和无限历史保留。
随后运行该聚焦测试和完整测试套件。
