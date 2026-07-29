#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
钉钉机器人通知工具
=================
支持钉钉自定义机器人 Webhook 的文本和 Markdown 消息发送。
支持三种安全方式: 加签、关键词、IP白名单。

使用方法:
    notifier = DingTalkNotifier(webhook_url, secret="SECxxx")
    notifier.send_text("雪球跟单告警: cookie 已失效")
    notifier.send_markdown("## 跟单状态报告\n- 运行时间: 2h\n- 异常: cookie失效")
"""

import hashlib
import hmac
import base64
import threading
import time
import urllib.parse
from collections import deque
from typing import Optional

import requests


class DingTalkNotifier:
    """钉钉机器人异步通知器 — 所有消息通过后台线程发送，不阻塞主流程"""

    def __init__(
        self,
        webhook_url: str,
        secret: Optional[str] = None,
    ):
        """
        :param webhook_url: 钉钉机器人 Webhook 地址
            https://oapi.dingtalk.com/robot/send?access_token=xxx
        :param secret: 加签密钥 (可选, 如果机器人设置了"加签"安全方式则必填)
        """
        self.webhook_url = webhook_url.strip()
        self.secret = secret
        self._session = requests.Session()
        self._session.verify = False
        self._pending = deque()  # 待发送队列
        self._lock = threading.Lock()
        self._worker_started = False
        self._running = True

    def _start_worker(self):
        """启动后台发送线程（幂等）"""
        if self._worker_started:
            return
        self._worker_started = True
        t = threading.Thread(target=self._send_loop, daemon=True)
        t.start()

    def _send_loop(self):
        """后台线程：从队列取消息发送"""
        while self._running:
            try:
                with self._lock:
                    if not self._pending:
                        pass  # 空队列, 继续等
                # 非空时取一条
                payload = None
                with self._lock:
                    if self._pending:
                        payload = self._pending.popleft()
                if payload is not None:
                    self._do_send(payload)
                else:
                    time.sleep(0.5)  # 队列空，等 0.5s 再检查
            except Exception:
                time.sleep(1)

    def _sign(self) -> tuple:
        """生成加签参数, 返回 (timestamp, sign)"""
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{self.secret}"
        string_to_sign_enc = string_to_sign.encode("utf-8")
        hmac_code = hmac.new(
            secret_enc, string_to_sign_enc, digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return timestamp, sign

    def _get_signed_url(self) -> str:
        """获取加签后的完整 URL"""
        if not self.secret:
            return self.webhook_url
        timestamp, sign = self._sign()
        separator = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{separator}timestamp={timestamp}&sign={sign}"

    def send_text(self, content: str, at_mobiles: list = None, at_all: bool = False) -> bool:
        """
        异步发送文本消息 (立即返回，后台线程发送)
        :param content: 文本内容
        :param at_mobiles: @指定手机号列表
        :param at_all: 是否 @所有人
        """
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"atMobiles": at_mobiles or [], "isAtAll": at_all},
        }
        self._enqueue(payload)
        return True

    def send_markdown(
        self,
        title: str,
        text: str,
        at_mobiles: list = None,
        at_all: bool = False,
    ) -> bool:
        """
        异步发送 Markdown 消息 (立即返回，后台线程发送)
        :param title: 消息标题
        :param text: Markdown 内容
        :param at_mobiles: @指定手机号列表
        :param at_all: 是否 @所有人
        """
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
            "at": {"atMobiles": at_mobiles or [], "isAtAll": at_all},
        }
        self._enqueue(payload)
        return True

    def _enqueue(self, payload: dict):
        """放入发送队列，首次调用时启动后台线程"""
        self._start_worker()
        with self._lock:
            # 防止队列无限膨胀，最多保留 20 条
            if len(self._pending) < 20:
                self._pending.append(payload)

    def _do_send(self, payload: dict):
        """实际发送请求到钉钉"""
        url = self._get_signed_url()
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("errcode") != 0:
                print(f"[钉钉通知] 发送失败: {result}")
        except Exception as e:
            print(f"[钉钉通知] 网络异常: {e}")


# ============================================================
# 快速发送函数 (无需创建对象, 直接调用)
# ============================================================

def send_dingtalk(
    webhook_url: str,
    content: str,
    secret: Optional[str] = None,
    is_markdown: bool = False,
) -> bool:
    """
    快速发送钉钉消息
    :param webhook_url: Webhook 地址
    :param content: 消息内容
    :param secret: 加签密钥
    :param is_markdown: 是否 Markdown 格式
    """
    notifier = DingTalkNotifier(webhook_url, secret)
    if is_markdown:
        return notifier.send_markdown("雪球跟单通知", content)
    else:
        return notifier.send_text(content)
