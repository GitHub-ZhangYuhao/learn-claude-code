#!/usr/bin/env python3
"""
slack_bot.py - Slack Bot 接入 Agent Team

Slack ↔ AgentTeam 沟通逻辑：
1. @Bot 消息 → 放入 _MainAgent_InputQueue → 触发 agent_loop
2. 监控 _AgentTeam_OutputPrint → 新内容推送到 Slack Thread
"""

import os
import threading
from time import sleep
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

# ── Slack Thread 管理 ─────────────────────────────────────────────────
# thread_ts -> {channel, client}
_active_threads: dict = {}


def _slack_output_monitor():
    """后台线程：消费 _AgentTeam_OutputPrint Queue，推送到 Slack。"""
    while True:
        item = _AgentTeam_OutputPrint.get()  # 阻塞等待
        agent_name = item.get("agent_name", "Unknown")
        content = item.get("content", "")
        if not content:
            continue
        # 推送到所有活跃的 Thread
        for thread_ts, info in list(_active_threads.items()):
            try:
                info["client"].chat_postMessage(
                    channel=info["channel"],
                    thread_ts=thread_ts,
                    text=f"*[{agent_name}]* {content}",
                )
            except Exception as e:
                print(f"[slack_bot] 推送失败: {e}")


def _get_teammate_manager():
    """获取 AIAgentIMP 模块中的 TeammateManager 实例。"""
    import AIAgentIMP
    return AIAgentIMP._TeammateManager


def _build_agent_buttons(channel: str, thread_ts: str) -> list:
    """动态构建 Agent 按钮列表。"""
    tm = _get_teammate_manager()
    buttons = []
    all_names = ["Leader"] + tm.member_names()
    for name in all_names:
        prop = tm.agent_Properties.get(name)
        status = "🟢" if (not prop or prop.isIdleStatus) else "🔴"
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"{status} {name}", "emoji": True},
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
    @Bot（无文本）→ 回复 Agent 按钮菜单
    @Bot 消息内容  → 直接发给 Leader
    """
    channel = event.get("channel")
    user = event.get("user")
    raw_text = event.get("text", "")
    thread_ts = event.get("ts")
    message = _strip_mention(raw_text)

    print(f"[slack_bot] @mention 用户={user} 消息={message!r}")

    if message:
        # 有跟随文本 → 直接发给 Leader
        _active_threads[thread_ts] = {"channel": channel, "client": client}
        _MainAgent_InputQueue.put({"role": "user", "content": message})
    else:
        # 无文本 → 显示 Agent 选择菜单
        buttons = _build_agent_buttons(channel, thread_ts)
        say(
            text="选择一个 Agent 开始对话：",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "*选择一个 Agent 开始对话：*"}},
                {"type": "actions", "elements": buttons},
            ],
            thread_ts=thread_ts,
        )


# ── 按钮点击 → 弹出 Modal ─────────────────────────────────────────────

def _make_button_handler(agent_name: str):
    def handler(ack, body, client):
        ack()
        value = body["actions"][0]["value"]
        channel, thread_ts = value.split("|", 1)
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "ask_agent_modal",
                "private_metadata": f"{agent_name}|{channel}|{thread_ts}",
                "title": {"type": "plain_text", "text": f"Ask {agent_name}"[:24]},
                "submit": {"type": "plain_text", "text": "发送"},
                "blocks": [{
                    "type": "input",
                    "block_id": "message_block",
                    "label": {"type": "plain_text", "text": f"向 {agent_name} 发送消息"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "message_input",
                        "multiline": True,
                        "placeholder": {"type": "plain_text", "text": "输入你的消息..."},
                    },
                }],
            },
        )
    return handler


def _register_agent_actions():
    """为所有已加载的 Agent 注册按钮 action handler。"""
    tm = _get_teammate_manager()
    all_names = ["Leader"] + tm.member_names()
    for name in all_names:
        app.action(f"select_agent_{name}")(_make_button_handler(name))


# ── Modal 提交 → 路由到 Agent ──────────────────────────────────────────

@app.view("ask_agent_modal")
def handle_modal_submit(ack, body, client):
    ack()
    meta = body["view"]["private_metadata"]
    agent_name, channel, thread_ts = meta.split("|", 2)
    message = body["view"]["state"]["values"]["message_block"]["message_input"]["value"]
    user_id = body["user"]["id"]

    # 注册活跃 Thread
    _active_threads[thread_ts] = {"channel": channel, "client": client}

    # 确认消息
    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"✅ <@{user_id}> → *{agent_name}*: {message}",
    )

    # 路由到 Agent
    tm = _get_teammate_manager()
    tm.send_message_to_agent(agent_name, message, f"SlackUser:{user_id}")


@app.event("message")
def handle_message(body, client, logger):
    event = body.get("event", {})
    print(f"[slack_bot] message事件: type={event.get('type')} subtype={event.get('subtype')} text={event.get('text', '')[:50]}")


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
