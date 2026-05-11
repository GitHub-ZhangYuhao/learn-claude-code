"""
FeishuAdapter.py — 飞书群聊通信适配器

封装所有飞书 SDK 交互逻辑：
- WebSocket 长连接接收消息（无需公网 IP）
- 交互卡片发送（不同 Agent 不同颜色 header）
- 消息路由（默认 Leader，支持文本 @AgentName 路由到子 Agent）
- message_id 消息去重

依赖: pip install lark-oapi
"""

import json
import re
import logging

import requests as _requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

logger = logging.getLogger(__name__)

# 子 Agent 卡片颜色池，Leader 固定为 green
_COLOR_POOL = ["blue", "purple", "orange", "turquoise", "violet", "wathet", "indigo"]


class FeishuAdapter:
    """
    飞书群聊通信适配器

    通过 lark_oapi WebSocket 长连接接收群消息，
    按 Agent 名称发送不同颜色的交互卡片到群聊。
    """

    def __init__(self, app_id: str, app_secret: str,
                 agent_webhooks: dict = None):
        """
        Args:
            app_id: 飞书应用 App ID（用于 WebSocket 接收消息 + 回退发送）
            app_secret: 飞书应用 App Secret
            agent_webhooks: {agent_name: webhook_url} 映射。
                每个 Agent 对应一个群自定义机器人的 Webhook URL，
                发消息时会以该机器人的身份出现在群里。
                未配置 webhook 的 Agent 回退为主 App Bot 发送卡片。
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self._chat_id = None                    # 最近一次收到消息的群 chat_id
        self._seen_msg_ids = set()              # 消息去重集合
        self._agent_color_map = {"Leader": "green"}
        self._color_idx = 0
        self._teammate_manager = None
        self._agent_webhooks = agent_webhooks or {}  # {agent_name: webhook_url}

        # 飞书 API Client（用于 WebSocket 接收 + 回退发送）
        self.api_client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .build()

        if self._agent_webhooks:
            logger.info("[飞书] 已配置 Webhook 机器人: %s",
                        list(self._agent_webhooks.keys()))

    def set_teammate_manager(self, tm):
        """注入 TeammateManager 实例，用于消息路由和 Agent 名称校验"""
        self._teammate_manager = tm

    def _get_agent_color(self, agent_name: str) -> str:
        """获取 Agent 对应的卡片 header 颜色"""
        if agent_name not in self._agent_color_map:
            self._agent_color_map[agent_name] = _COLOR_POOL[self._color_idx % len(_COLOR_POOL)]
            self._color_idx += 1
        return self._agent_color_map[agent_name]

    # ----------------------------------------------------------------
    # 接收消息
    # ----------------------------------------------------------------

    def _on_receive_message(self, data) -> None:
        """
        飞书事件回调: im.message.receive_v1

        处理流程:
        1. 消息去重
        2. 提取文本内容
        3. 去除飞书 @bot 占位符
        4. 解析文本中的 @AgentName 路由
        5. 投入对应 Agent 的消息队列
        """
        try:
            msg = data.event.message
            message_id = msg.message_id

            # 消息去重
            if message_id in self._seen_msg_ids:
                return
            self._seen_msg_ids.add(message_id)
            if len(self._seen_msg_ids) > 1000:
                self._seen_msg_ids = set(list(self._seen_msg_ids)[-500:])

            # 只处理文本消息
            if msg.message_type != "text":
                return

            # 记录当前群 chat_id，后续发消息用
            self._chat_id = msg.chat_id

            content = json.loads(msg.content)
            text = content.get("text", "").strip()
            if not text:
                return

            # 解析飞书 mention 列表，识别目标 Agent
            target_agent = "Leader"
            mentions = getattr(msg, "mentions", None)
            if mentions:
                for mention in mentions:
                    key = getattr(mention, "key", "")
                    mention_name = getattr(mention, "name", "")
                    if not key:
                        continue
                    # 检查 mention 的名字是否匹配已知 Agent
                    if (self._teammate_manager
                            and mention_name in self._teammate_manager.agent_Properties):
                        target_agent = mention_name
                    # 也检查 webhook 机器人名字是否匹配
                    elif mention_name in self._agent_webhooks:
                        target_agent = mention_name
                    # 从文本中去掉占位符
                    text = text.replace(key, "").strip()

            # 兜底：解析文本中手动输入的 @AgentName 路由
            if target_agent == "Leader":
                at_match = re.match(r"^@(\w+)\s+(.*)", text, re.DOTALL)
                if at_match and self._teammate_manager:
                    potential = at_match.group(1)
                    if potential in self._teammate_manager.agent_Properties:
                        target_agent = potential
                        text = at_match.group(2).strip()
                    elif potential in self._agent_webhooks:
                        target_agent = potential
                        text = at_match.group(2).strip()

            logger.info("[飞书收到] chat=%s target=%s text=%s",
                        self._chat_id, target_agent, text[:80])

            # 投入 Agent 消息队列
            if self._teammate_manager:
                self._teammate_manager.send_message_to_agent(target_agent, text, "User")

        except Exception as e:
            logger.error("[飞书 on_message 异常] %s", e, exc_info=True)

    # ----------------------------------------------------------------
    # 发送消息
    # ----------------------------------------------------------------

    def send_card(self, agent_name: str, content: str,
                  tools_summary: str = "", chat_id: str = None):
        """
        发送飞书交互卡片消息到群聊

        卡片结构:
        ┌─ 🤖 AgentName (颜色 header) ─┐
        │  content (markdown)            │
        │  ─────────────────             │  (仅当 tools_summary 非空时)
        │  ⚙️ tool1                      │
        │  ⚙️ tool2                      │
        └────────────────────────────────┘

        Args:
            agent_name: Agent 名称，决定卡片颜色
            content: 消息正文（支持飞书 markdown 子集）
            tools_summary: 工具调用摘要（可选）
            chat_id: 目标群 ID，默认使用最近收到消息的群
        """
        color = self._get_agent_color(agent_name)

        elements = [{"tag": "markdown", "content": content[:2000]}]
        if tools_summary:
            elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": tools_summary[:1000]})

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": f"🤖 {agent_name}"},
                "template": color,
            },
            "elements": elements,
        }

        # 优先走 Webhook 自定义机器人（独立身份）
        webhook_url = self._agent_webhooks.get(agent_name)
        if webhook_url:
            self._send_via_webhook(webhook_url, card)
            return

        # 回退：用主 App Bot 通过 API 发送卡片（需要 chat_id）
        chat_id = chat_id or self._chat_id
        if not chat_id:
            logger.warning("[飞书] 无 chat_id 且无 webhook，跳过: %s: %s", agent_name, content[:50])
            return
        self._send_via_api(chat_id, card)

    def _send_via_webhook(self, webhook_url: str, card: dict):
        """
        通过群自定义机器人 Webhook 发送卡片。
        每个 Webhook 机器人有独立的名字和头像，在群里表现为不同的实体。
        """
        payload = {
            "msg_type": "interactive",
            "card": card,
        }
        try:
            resp = _requests.post(webhook_url, json=payload, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                logger.error("[Webhook发送失败] %s", result)
        except Exception as e:
            logger.error("[Webhook发送异常] %s", e, exc_info=True)

    def _send_via_api(self, chat_id: str, card: dict):
        """通过主 App Bot API 发送卡片（回退方式）"""
        try:
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(json.dumps(card))
                    .build()
                ).build()

            resp = self.api_client.im.v1.message.create(req)
            if not resp.success():
                logger.error("[飞书API发送失败] code=%s msg=%s", resp.code, resp.msg)
        except Exception as e:
            logger.error("[飞书API发送异常] %s", e, exc_info=True)

    # ----------------------------------------------------------------
    # 启动
    # ----------------------------------------------------------------

    def start(self):
        """启动飞书 WebSocket 长连接（阻塞当前线程）"""
        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_receive_message) \
            .build()

        ws = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.DEBUG,
        )
        logger.info("[飞书] WebSocket 长连接启动中...")
        ws.start()
