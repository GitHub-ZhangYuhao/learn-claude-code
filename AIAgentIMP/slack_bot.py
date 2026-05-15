#!/usr/bin/env python3
"""
slack_bot.py - Slack Bot WebSocket Relay Server

架构：
  Slack ↔ 本服务器 (WebSocket Server) ↔ 各用户本地 AgentTeam (WebSocket Client)

流程：
1. 用户 @Bot 消息 → 本服务器收到 → 通过 WebSocket 转发给对应用户的本地 Agent
2. 本地 Agent 处理完毕 → 通过 WebSocket 回传输出 → 本服务器推送到 Slack
"""

import os
import re
import json
import asyncio
import threading
from time import time, strftime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import websockets
from websockets.asyncio.server import serve as ws_serve

load_dotenv()

app = App(token=os.getenv("SLACK_BOT_TOKEN"))

# WebSocket 服务器配置
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("WS_PORT", "8765"))

# ── Slack Block Kit 卡片构建 ──────────────────────────────────────────

# 消息类型 → emoji 映射
_MSG_TYPE_EMOJIS = {
    "text": "💬",
    "tool_call": "🔧",
    "tool_result": "📄",
    "error": "❌",
    "system": "⚙️",
}

# Agent 角色 → 颜色/图标映射（用于 header 装饰）
_AGENT_ROLE_DECORATIONS = {
    "Leader": ("🧑‍✈️", "#439FE0"),
    "Coder": ("👨‍💻", "#36a64f"),
    "Reviewer": ("🔍", "#e01e5a"),
    "default": ("🤖", "#555555"),
}

# tool_call 参数预览最大长度
_TOOL_CALL_ARGS_PREVIEW = 120

# 内容截断阈值（Slack 单个 block text 上限约 3000 字符）
_TEXT_TRUNCATE_LIMIT = 800
_TOOL_RESULT_TRUNCATE_LIMIT = 500


class _SlackCardBuilder:
    """流式聚合 Slack Block Kit 卡片。

    接收单条消息，内部按 agent_name 分组缓冲，
    超时后 flush 返回完整的 blocks 列表。
    """

    def __init__(self, flush_delay: float = 1.5):
        self._flush_delay = flush_delay
        self._buffer: dict = {}

    def add_message(self, thread_ts: str, agent_name: str, msg_type: str, content: str) -> list | None:
        """添加一条消息，如果触发 flush 则返回 blocks，否则返回 None。"""
        if thread_ts not in self._buffer:
            self._buffer[thread_ts] = {"_flush_at": time() + self._flush_delay}

        buf = self._buffer[thread_ts]
        agent_key = f"agent:{agent_name}"
        if agent_key not in buf:
            buf[agent_key] = []
        buf[agent_key].append((msg_type, content))

        if time() >= buf["_flush_at"]:
            return self._flush(thread_ts)
        return None

    def force_flush(self, thread_ts: str) -> list | None:
        """强制 flush 指定 thread 的缓冲。"""
        if thread_ts in self._buffer:
            return self._flush(thread_ts)
        return None

    def _flush(self, thread_ts: str) -> list | None:
        if thread_ts not in self._buffer:
            return None

        buf = self._buffer.pop(thread_ts)
        agents = {}
        for key, items in buf.items():
            if key.startswith("agent:"):
                agents[key[len("agent:"):]] = items

        if not agents:
            return None

        blocks = []
        for agent_name, items in agents.items():
            blocks.extend(self._build_agent_card(agent_name, items))

        return blocks

    def _build_agent_card(self, agent_name: str, items: list) -> list:
        """为单个 Agent 构建 Block Kit blocks。"""
        blocks = []

        emoji, color = _AGENT_ROLE_DECORATIONS.get(agent_name, _AGENT_ROLE_DECORATIONS["default"])

        # 统计消息类型
        type_counts = {}
        for mt, _ in items:
            type_counts[mt] = type_counts.get(mt, 0) + 1
        type_summary = "  ".join(
            f"{_MSG_TYPE_EMOJIS.get(t, '📌')}{c}" for t, c in type_counts.items()
        )

        blocks.append({
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {agent_name}",
                "emoji": True,
            },
        })

        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"`{agent_name}` · {type_summary} · :clock1: {strftime('%H:%M:%S')}",
            }],
        })

        for msg_type, content in items:
            emoji = _MSG_TYPE_EMOJIS.get(msg_type, "📌")

            if msg_type == "text":
                display = content if len(content) <= _TEXT_TRUNCATE_LIMIT else content[:_TEXT_TRUNCATE_LIMIT] + "..."
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{emoji} {display}"},
                })

            elif msg_type == "tool_call":
                tool_name, args_preview = self._extract_tool_info(content)
                tool_label = f"{emoji} *工具调用:* `{tool_name}`" if tool_name else f"{emoji} *工具调用*"
                if args_preview:
                    blocks.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{tool_label}\n```\n{args_preview}\n```",
                        },
                    })
                else:
                    blocks.append({
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": tool_label,
                        },
                    })

            elif msg_type == "tool_result":
                display = content if len(content) <= _TOOL_RESULT_TRUNCATE_LIMIT else content[:_TOOL_RESULT_TRUNCATE_LIMIT] + "..."
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *工具返回:*\n```\n{display}\n```",
                    },
                })

            elif msg_type == "error":
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *错误:*\n```\n{content}\n```",
                    },
                })

            elif msg_type == "system":
                blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"{emoji} {content}"}],
                })

        blocks.append({"type": "divider"})
        return blocks

    @staticmethod
    def _extract_tool_name(content: str) -> str:
        """从 tool_call 内容中提取工具名称。"""
        match = re.search(r"使用工具[：:]\s*(\w+)", content)
        if match:
            return match.group(1)
        return ""

    def _extract_tool_info(self, content: str) -> tuple[str, str]:
        """从 tool_call 内容中提取工具名称和参数预览。"""
        tool_name = self._extract_tool_name(content)
        args_match = re.search(r"参数[：:]\s*(\{.*?\}|\[.*?\]|[^{}\[\]]+)", content, re.DOTALL)
        args_preview = ""
        if args_match:
            args_preview = args_match.group(1).strip()[:_TOOL_CALL_ARGS_PREVIEW]
            if len(args_match.group(1)) > _TOOL_CALL_ARGS_PREVIEW:
                args_preview += "..."
        return tool_name, args_preview


# 全局实例
_slack_card_builder = _SlackCardBuilder(flush_delay=1.0)


# ── WebSocket 连接管理 ─────────────────────────────────────
# user_id -> {"ws": websocket, "agents": ["Leader", "Coder", ...]}
_ws_clients: dict = {}
_ws_clients_lock = threading.Lock()
_ws_loop: asyncio.AbstractEventLoop = None  # WebSocket 事件循环引用


# ── Slack Thread 管理 ─────────────────────────────────────────
# thread_ts -> {channel, client, "_last_flush", "target_agent", "user_id"}
_active_threads: dict = {}
_THREAD_FLUSH_INTERVAL = 2.0


def _get_or_create_thread(thread_ts: str, channel: str, client, user_id: str = "", target_agent: str = "Leader") -> dict:
    """获取或创建 thread 信息，保留已有 target_agent。"""
    if thread_ts in _active_threads:
        info = _active_threads[thread_ts]
        info["channel"] = channel
        info["client"] = client
        if user_id:
            info["user_id"] = user_id
        return info
    info = {
        "channel": channel,
        "client": client,
        "_last_flush": time(),
        "target_agent": target_agent,
        "user_id": user_id,
    }
    _active_threads[thread_ts] = info
    return info


def _send_to_local_agent(user_id: str, message: dict) -> bool:
    """通过 WebSocket 发送消息给用户的本地 Agent。返回是否成功。"""
    with _ws_clients_lock:
        client_info = _ws_clients.get(user_id)
    if not client_info or not _ws_loop:
        return False
    ws = client_info["ws"]
    try:
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(message)), _ws_loop)
        return True
    except Exception as e:
        print(f"[relay] WebSocket 发送失败 user={user_id}: {e}")
        return False


def _post_agent_output_to_slack(data: dict):
    """将本地 Agent 回传的输出推送到 Slack thread。"""
    thread_ts = data.get("thread_ts")
    agent_name = data.get("agent_name", "Unknown")
    msg_type = data.get("msg_type", "text")
    content = data.get("content", "")
    if not thread_ts or not content:
        return

    info = _active_threads.get(thread_ts)
    if not info:
        return

    blocks = _slack_card_builder.add_message(thread_ts, agent_name, msg_type, content)
    if blocks:
        try:
            info["client"].chat_postMessage(
                channel=info["channel"],
                thread_ts=thread_ts,
                text=f"[{agent_name}] {content[:80]}",
                blocks=blocks,
            )
            info["_last_flush"] = time()
        except Exception as e:
            print(f"[relay] Slack 推送失败: {e}")
    else:
        now = time()
        if now - info.get("_last_flush", 0) > _THREAD_FLUSH_INTERVAL:
            blocks = _slack_card_builder.force_flush(thread_ts)
            if blocks:
                try:
                    info["client"].chat_postMessage(
                        channel=info["channel"],
                        thread_ts=thread_ts,
                        text=f"[{agent_name}] 新消息",
                        blocks=blocks,
                    )
                    info["_last_flush"] = now
                except Exception as e:
                    print(f"[relay] Slack 推送失败: {e}")


def _build_agent_buttons(channel: str, thread_ts: str, user_id: str) -> list:
    """根据用户本地 Agent 列表构建按钮。"""
    with _ws_clients_lock:
        client_info = _ws_clients.get(user_id)
    if not client_info:
        return []
    agent_names = client_info.get("agents", ["Leader"])
    buttons = []
    for name in agent_names:
        role_emoji, _ = _AGENT_ROLE_DECORATIONS.get(name, _AGENT_ROLE_DECORATIONS["default"])
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"{role_emoji} {name}", "emoji": True},
            "action_id": f"select_agent_{name}",
            "value": f"{channel}|{thread_ts}",
        })
    return buttons


def _strip_mention(text: str) -> str:
    """去掉 @Bot mention 标记，返回纯用户消息。"""
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


# ── Slack 事件处理 ────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, client):
    """
    @Bot（无文本）  → 回复 Agent 按钮菜单
    @Bot 消息内容 → 通过 WebSocket 转发给用户本地 Agent
    """
    channel = event.get("channel")
    user = event.get("user")
    raw_text = event.get("text", "")
    thread_ts = event.get("ts")
    message = _strip_mention(raw_text)

    print(f"[relay] @mention 用户={user} 消息={message!r}")

    # 检查用户是否有本地 Agent 在线
    with _ws_clients_lock:
        is_online = user in _ws_clients

    if not is_online:
        say(
            text=f"❌ <@{user}> 你的本地 Agent 未连接。请先在本地运行 `python local_agent.py`",
            thread_ts=thread_ts,
        )
        return

    if not message:
        buttons = _build_agent_buttons(channel, thread_ts, user)
        say(
            text="选择一个 Agent 开始对话",
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "🤖 Agent Team", "emoji": True},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "点击按钮选择一个 Agent 开始对话，或直接 `@Bot 消息` 发送给当前目标 Agent：",
                    },
                },
                {"type": "actions", "elements": buttons},
            ],
            thread_ts=thread_ts,
        )
        return

    # 普通消息：通过 WebSocket 转发给本地 Agent
    info = _get_or_create_thread(thread_ts, channel, client, user_id=user)
    target = info["target_agent"]
    _send_to_local_agent(user, {
        "type": "user_message",
        "thread_ts": thread_ts,
        "channel": channel,
        "user_id": user,
        "text": message,
        "target_agent": target,
    })


@app.event("message")
def handle_message(event, client):
    """
    处理 thread 中用户的后续消息（不带 @mention）→ 通过 WebSocket 转发
    """
    channel = event.get("channel")
    user = event.get("user")
    raw_text = event.get("text", "")
    thread_ts = event.get("thread_ts")

    if not thread_ts:
        return

    if raw_text.strip().startswith("<@"):
        return

    message = _strip_mention(raw_text)
    if not message:
        return

    print(f"[relay] thread消息 用户={user} 消息={message!r}")

    info = _get_or_create_thread(thread_ts, channel, client, user_id=user)
    target = info["target_agent"]
    _send_to_local_agent(user, {
        "type": "user_message",
        "thread_ts": thread_ts,
        "channel": channel,
        "user_id": user,
        "text": message,
        "target_agent": target,
    })


# ── 按钮点击 → 切换目标 Agent ───────────────────────────────

# 动态注册的 action 名称集合，避免重复注册
_registered_actions: set = set()


def _make_button_handler(agent_name: str):
    def handler(ack, body, client):
        ack()
        value = body["actions"][0]["value"]
        channel, thread_ts = value.split("|", 1)
        user_id = body.get("user", {}).get("id", "")

        info = _get_or_create_thread(thread_ts, channel, client, user_id=user_id, target_agent=agent_name)
        info["target_agent"] = agent_name

        role_emoji, _ = _AGENT_ROLE_DECORATIONS.get(agent_name, _AGENT_ROLE_DECORATIONS["default"])
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"✅ 已选择 {role_emoji} *{agent_name}* 作为对话目标\n"
                f"直接在此 thread 中发消息即可与 *{agent_name}* 对话"
            ),
        )
    return handler


def _ensure_action_registered(agent_name: str):
    """确保指定 Agent 的按钮 action 已注册。"""
    action_id = f"select_agent_{agent_name}"
    if action_id not in _registered_actions:
        app.action(action_id)(_make_button_handler(agent_name))
        _registered_actions.add(action_id)


# ── WebSocket 服务器 ────────────────────────────────────────

async def _ws_handler(websocket):
    """处理单个 WebSocket 客户端连接。"""
    user_id = None
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # ── 注册：本地 Agent 上线 ──
            if msg_type == "register":
                user_id = data.get("user_id", "")
                agents = data.get("agents", ["Leader"])
                with _ws_clients_lock:
                    _ws_clients[user_id] = {"ws": websocket, "agents": agents}
                # 为该用户的 Agent 动态注册按钮 action
                for name in agents:
                    _ensure_action_registered(name)
                print(f"[relay] 用户 {user_id} 已连接，Agent列表: {agents}")
                await websocket.send(json.dumps({"type": "registered", "user_id": user_id}))

            # ── Agent 输出：本地 Agent → Slack ──
            elif msg_type == "agent_output":
                _post_agent_output_to_slack(data)

    except websockets.ConnectionClosed:
        pass
    finally:
        if user_id:
            with _ws_clients_lock:
                _ws_clients.pop(user_id, None)
            print(f"[relay] 用户 {user_id} 已断开")


async def _run_ws_server():
    """WebSocket 服务器主循环。"""
    async with ws_serve(_ws_handler, WS_HOST, WS_PORT):
        print(f"[relay] WebSocket 服务器已启动: ws://{WS_HOST}:{WS_PORT}")
        await asyncio.Future()  # 永久运行


def _start_ws_server():
    """在独立线程中启动 WebSocket 服务器。"""
    global _ws_loop
    _ws_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ws_loop)
    _ws_loop.run_until_complete(_run_ws_server())


# ── 启动 ──────────────────────────────────────────────

def main():
    # 启动 WebSocket 服务器线程
    threading.Thread(target=_start_ws_server, daemon=True, name="WebSocketServer").start()

    # 启动 Slack Socket Mode
    socket_token = os.getenv("SLACK_SOCKET_TOKEN")
    if not socket_token:
        print("错误: 未设置 SLACK_SOCKET_TOKEN")
        return
    print("[relay] Slack Bot Relay 已启动")
    SocketModeHandler(app, socket_token).start()


if __name__ == "__main__":
    main()
