#!/usr/bin/env python3
"""
slack_bot.py - Slack Bot 接入 Agent Team

Slack ↔ AgentTeam 沟通逻辑：
1. @Bot 消息 → 放入 _MainAgent_InputQueue → 触发 agent_loop
2. 监控 _AgentTeam_OutputPrint → 新内容推送到 Slack Thread
"""

import os
import threading
from time import time, strftime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# 加载 .env
load_dotenv()

# 导入 AIAgentIMP 全部基础设施
import re
from AIAgentIMP import AgentTeamMain, _MainAgent_InputQueue, _AgentTeam_OutputPrint
from TeammateManager import TeammateManager

app = App(token=os.getenv("SLACK_BOT_TOKEN"))

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


# ── Slack Thread 管理 ─────────────────────────────────────────────────
# thread_ts -> {channel, client, "_last_flush", "target_agent"}
_active_threads: dict = {}
_THREAD_FLUSH_INTERVAL = 2.0  # 定期 flush 的间隔


def _get_or_create_thread(thread_ts: str, channel: str, client, target_agent: str = "Leader") -> dict:
    """获取或创建 thread 信息，保留已有 target_agent。"""
    if thread_ts in _active_threads:
        info = _active_threads[thread_ts]
        info["channel"] = channel
        info["client"] = client
        return info
    info = {
        "channel": channel,
        "client": client,
        "_last_flush": time(),
        "target_agent": target_agent,
    }
    _active_threads[thread_ts] = info
    return info


def _slack_output_monitor():
    """后台线程：消费 _AgentTeam_OutputPrint Queue，推送到 Slack。"""
    while True:
        item = _AgentTeam_OutputPrint.get()  # 阻塞等待
        agent_name = item.get("agent_name", "Unknown")
        msg_type = item.get("msg_type", "text")
        content = item.get("content", "")
        if not content:
            continue

        # 推送到所有活跃的 Thread（通过卡片构建器聚合）
        for thread_ts, info in list(_active_threads.items()):
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
                    print(f"[slack_bot] 推送失败: {e}")
            else:
                # 没触发 flush，检查是否需要强制 flush（超时兜底）
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
                            print(f"[slack_bot] 推送失败: {e}")


def _get_teammate_manager():
    """获取 AIAgentIMP 模块中的 TeammateManager 实例。"""
    import AIAgentIMP
    return AIAgentIMP._TeammateManager


def _build_agent_buttons(channel: str, thread_ts: str) -> list:
    """动态构建 Agent 按钮列表。"""
    tm = _get_teammate_manager()
    all_names = ["Leader"] + tm.member_names()
    buttons = []
    for name in all_names:
        prop = tm.agent_Properties.get(name)
        is_idle = not prop or prop.isIdleStatus
        status_icon = "🟢" if is_idle else "🔴"
        role_emoji, _ = _AGENT_ROLE_DECORATIONS.get(name, _AGENT_ROLE_DECORATIONS["default"])
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"{status_icon}{role_emoji} {name}", "emoji": True},
            "action_id": f"select_agent_{name}",
            "value": f"{channel}|{thread_ts}",
        })
    return buttons


def _strip_mention(text: str) -> str:
    """去掉 @Bot mention 标记，返回纯用户消息。"""
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


# ── Slack 事件处理 ────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, client):
    """
    @Bot（无文本）  → 回复 Agent 按钮菜单
    @Bot 消息内容 → 发给当前 thread 目标 Agent（默认 Leader）
    """
    channel = event.get("channel")
    user = event.get("user")
    raw_text = event.get("text", "")
    thread_ts = event.get("ts")
    message = _strip_mention(raw_text)

    print(f"[slack_bot] @mention 用户={user} 消息={message!r}")

    if not message:
        buttons = _build_agent_buttons(channel, thread_ts)
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

    # 普通消息：发送给当前 thread 目标 Agent
    info = _get_or_create_thread(thread_ts, channel, client)
    target = info["target_agent"]
    tm = _get_teammate_manager()
    tm.send_message_to_agent(target, message, f"SlackUser:{user}")


@app.event("message")
def handle_message(event, client):
    """
    处理 thread 中用户的后续消息（不带 @mention）→ 路由到 thread 的 target_agent
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

    print(f"[slack_bot] thread消息 用户={user} 消息={message!r}")

    info = _get_or_create_thread(thread_ts, channel, client)
    target = info["target_agent"]
    tm = _get_teammate_manager()
    tm.send_message_to_agent(target, message, f"SlackUser:{user}")


# ── 按钮点击 → 切换目标 Agent ───────────────────────────────

def _make_button_handler(agent_name: str):
    def handler(ack, body, client):
        ack()
        value = body["actions"][0]["value"]
        channel, thread_ts = value.split("|", 1)

        info = _get_or_create_thread(thread_ts, channel, client, target_agent=agent_name)
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


def _register_agent_actions():
    """为所有已加载的 Agent 注册按钮 action handler。"""
    tm = _get_teammate_manager()
    all_names = ["Leader"] + tm.member_names()
    for name in all_names:
        app.action(f"select_agent_{name}")(_make_button_handler(name))


# ── 启动 ──────────────────────────────────────────────────────────────

def main():
    # 启动 AgentTeam 输出监控线程
    threading.Thread(target=_slack_output_monitor, daemon=True, name="SlackOutputMonitor").start()

    # 启动 AgentTeam 主循环（daemon 线程）
    threading.Thread(target=AgentTeamMain, args=(False,), daemon=True, name="AgentTeamThread").start()

    # 注册所有 Agent 按钮 action handler
    _register_agent_actions()

    # 启动 Slack Socket Mode
    socket_token = os.getenv("SLACK_SOCKET_TOKEN")
    if not socket_token:
        print("错误: 未设置 SLACK_SOCKET_TOKEN")
        return
    print("[slack_bot] Bot 已启动")
    SocketModeHandler(app, socket_token).start()


if __name__ == "__main__":
    main()
