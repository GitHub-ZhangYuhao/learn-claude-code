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
from AIAgentIMP import AgentTeamMain, _MainAgent_InputQueue, _AgentTeam_OutputPrint

app = App(token=os.getenv("SLACK_API_TOKEN"))

# ── Slack Thread 管理 ─────────────────────────────────────────────────
# thread_ts -> {channel, client, last_idx}
_active_threads: dict = {}


def _slack_output_monitor():
    """后台线程：消费 _AgentTeam_OutputPrint Queue，推送到 Slack。"""
    while True:
        sleep(1)
        item = _AgentTeam_OutputPrint.get()  # 阻塞等待，有新数据自动唤醒
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


# ── Slack 事件处理 ────────────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, client):
    """收到 @Bot 消息 → 放入 InputQueue，触发 AgentTeam 处理。"""
    channel = event.get("channel")
    user = event.get("user")
    text = event.get("text", "")
    thread_ts = event.get("ts")
    print(f"[slack_bot] @mention 用户={user} 消息={text}")

    # 注册活跃 Thread
    _active_threads[thread_ts] = {"channel": channel, "client": client, "last_idx": 0}

    # 放入 InputQueue，触发 agent_loop
    _MainAgent_InputQueue.put({"role": "user", "content": text})

    #say(f"收到: {text}", thread_ts=thread_ts)


@app.command("/add")
def handle_agent_command(ack, body, respond, client):
    """斜杠命令：获取 AgentTeam 最新输出。"""
    ack()
    respond("输出请查看实时消息")


@app.event("message")
def handle_message(body, client, logger):
    pass


# ── 启动 ──────────────────────────────────────────────────────────────

def main():
    # 启动 AgentTeam 输出监控线程
    threading.Thread(target=_slack_output_monitor, daemon=True, name="SlackOutputMonitor").start()

    # 启动 AgentTeam 主循环（daemon 线程）
    threading.Thread(target=AgentTeamMain, args=(False,) ,daemon=True, name="AgentTeamThread").start()

    # 启动 Slack Socket Mode
    socket_token = os.getenv("SLACK_SOCKET_TOKEN")
    if not socket_token:
        print("错误: 未设置 SLACK_SOCKET_TOKEN")
        return
    print("[slack_bot] Bot 已启动")
    SocketModeHandler(app, socket_token).start()


if __name__ == "__main__":
    main()
