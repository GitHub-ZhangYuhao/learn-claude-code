#!/usr/bin/env python3
"""
slack_adapter.py - Slack Bot 主入口

交互方式：
  1. 用户 @Bot → Bot 回复 Agent 按钮菜单
  2. 用户点击按钮 → 弹出 Modal 输入消息
  3. 提交后路由到对应 Agent，输出实时推送到 Thread
"""

import os
import sys
import re
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# 切换工作目录到 AIAgentIMP 项目根目录（GlobalConfig 依赖 cwd）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(_PROJECT_ROOT)
sys.path.insert(0, _PROJECT_ROOT)

# 加载 slack/.env（Slack Token），再加载项目根 .env（OpenAI Key 等）
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

from GlobalConfig import _OutputCallbacks
from output_sink import SlackOutputSink

# ── 初始化 AIAgentIMP 框架（复用现有模块） ──────────────────────────
import json
from time import sleep
from GlobalConfig import *
from GlobalConfig import _MainAgent_InputQueue, _MainAgent_IdleStatus, _MainAgent_Lock, _OutputCallbacks
from TeammateManager import TeammateManager
from SubAgentLoader import SubAgentLoader
from SystemPromptBuilder import SystemPromptBuilder
from ErrorRecovery import ErrorRecoveryManager
from TodoManager import TODO_TOOL_SCHEMA, TodoManager
from DefaultToolManager import BASIC_TOOLS, BASIC_TOOL_HANDLERS
from MemoryManager import MEMORY_SAVE_MEMORY_TOOL_HANDLERS, MEMORY_MANAGER_TOOL_SCHEMA
from MCPManager import MCPManager
from HookManager import *
from SkillManager import SkillRegistry

_TeammateManager = TeammateManager()

# 加载 .agent/ 下的所有子 Agent
_SubAgentLoader = SubAgentLoader()
for cfg_name, config in _SubAgentLoader.load().items():
    _TeammateManager.spawn(
        name=config.name,
        role=config.description,
        MCPs=config.MCPs,
        skills=config.skills,
        agent_detail=config.detail,
    )

# ── Leader Agent Loop 基础设施 ────────────────────────────────────────
_MainAgent_TODO = TodoManager()
_MainAgent_HOOKS = HookManager()
_MCPManager_Main = MCPManager()
_MCPManager_Main.connect_to_servers()

TOOL_HANDLERS = BASIC_TOOL_HANDLERS.copy()
TOOL_HANDLERS["todo"] = lambda **kw: _MainAgent_TODO.update(kw["items"])
TOOL_HANDLERS["spawn_teammate"] = lambda **kw: _TeammateManager.spawn(kw["name"], kw["role"], kw.get("prompt"))
TOOL_HANDLERS["list_teammates"] = lambda **kw: _TeammateManager.list_all()
TOOL_HANDLERS["send_message_to_agent"] = lambda **kw: _TeammateManager.send_message_to_agent(kw["agent_name"], kw["prompt"], kw["send_from"])
TOOL_HANDLERS.update(MEMORY_SAVE_MEMORY_TOOL_HANDLERS)

from TeammateManager import TEAMMATE_TOOL_SCHEMA, SPAWN_AGENT_TOOL_SCHEMA
TOOLS = BASIC_TOOLS + TODO_TOOL_SCHEMA + TEAMMATE_TOOL_SCHEMA + SPAWN_AGENT_TOOL_SCHEMA + MEMORY_MANAGER_TOOL_SCHEMA + _MCPManager_Main.get_tools_schema()

_MainAgent_Skills = SkillRegistry(SKILLS_DIR)
_SystemPromptManger = SystemPromptBuilder(workdir=WORKDIR, tools=TOOLS, skill_registry=_MainAgent_Skills)


def MainAgentPrint(text: str, msg_type: str = "text"):
    """主 Agent 输出函数，同时推送到外部前端（如 Slack）。"""
    print(text)
    cb = _OutputCallbacks.get("Leader")
    if cb:
        cb("Leader", msg_type, text)


def _leader_agent_loop(messages: list):
    """Leader 的 agent loop（与 AIAgentIMP.py 中逻辑一致）。"""
    todo = TodoManager()
    error_recovery = ErrorRecoveryManager()

    while True:
        messages = _SystemPromptManger.setup_system_prompt(messages)

        try:
            response = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, max_tokens=1000,
            )
            recover_decision = error_recovery.choose_recovery(response.choices[0].finish_reason, None)
        except Exception as e:
            recover_decision = error_recovery.choose_recovery(None, str(e).lower())

        msg = response.choices[0].message.content
        if msg:
            messages.append({"role": "assistant", "content": msg})
            MainAgentPrint(msg)

        has_error, need_continue, messages = error_recovery.recovery_by_decision(recover_decision, messages, attempt=1)
        if has_error:
            if need_continue:
                continue
            else:
                break

        if response.choices[0].finish_reason != "tool_calls":
            return

        for ToolCall in response.choices[0].message.tool_calls:
            tool_name = ToolCall.function.name
            tool_args = json.loads(ToolCall.function.arguments)
            tool_id = ToolCall.id

            hook_ctx = {"tool_name": tool_name, "tool_input": tool_args}
            pre_hook = _MainAgent_HOOKS.run_hooks(HOOK_EVENTS[1], hook_ctx)
            messages = append_hook_result_to_messages(pre_hook, tool_id, messages)
            should_block, messages = hook_should_block_tool_use(pre_hook, tool_id, messages)
            if should_block:
                continue

            if tool_name in _MCPManager_Main.get_mcp_tool_names():
                output = _MCPManager_Main.call_tool(tool_name, tool_args)
            else:
                handler = TOOL_HANDLERS.get(tool_name)
                output = handler(**tool_args) if handler else f"Unknown Tool: {tool_name}"
            MainAgentPrint(f"> \n使用工具：{tool_name} : 参数：{tool_args}", "tool_call")
            MainAgentPrint(str(output)[:500], "tool_result")

            hook_ctx["tool_output"] = output
            post_hook = _MainAgent_HOOKS.run_hooks(HOOK_EVENTS[2], hook_ctx)
            messages = append_hook_result_to_messages(post_hook, tool_id, messages)

            todo.check_used_todo_tool(tool_name)
            messages.append({"role": "tool", "tool_call_id": ToolCall.id, "content": output})

        messages = todo.post_tool_call(messages)


def _leader_consumer_thread():
    """消费 _MainAgent_InputQueue，为 Leader 运行 agent loop。"""
    history = []
    while True:
        if not _MainAgent_InputQueue.empty():
            user_query = _MainAgent_InputQueue.get()
            history.append(user_query)
            with _MainAgent_Lock:
                global _MainAgent_IdleStatus
                _MainAgent_IdleStatus = False
            try:
                _leader_agent_loop(history)
            except Exception as e:
                print(f"[Leader] agent loop 异常: {e}")
            finally:
                with _MainAgent_Lock:
                    _MainAgent_IdleStatus = True
                _OutputCallbacks.pop("Leader", None)
        sleep(1)


print("[SlackAdapter] Agent 列表:", _TeammateManager.list_all())

# ── Slack App ────────────────────────────────────────────────────────

app = App(token=os.getenv("SLACK_API_TOKEN"))

# 用于 Modal 提交后发消息的默认频道（从 .env 读取，或在 mention 时动态获取）
DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL", "")


def _build_agent_buttons(channel: str, thread_ts: str) -> list:
    """动态构建 Agent 按钮列表。"""
    buttons = []
    all_names = ["Leader"] + _TeammateManager.member_names()
    for name in all_names:
        prop = _TeammateManager.agent_Properties.get(name)
        if prop:
            status = "🟢" if prop.isIdleStatus else "🔴"
        else:
            status = "🟢"
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"{status} {name}", "emoji": True},
            "action_id": f"select_agent_{name}",
            "value": f"{channel}|{thread_ts}",
        })
    return buttons


# ── 1. @Bot → 回复 Agent 按钮菜单 ────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, client):
    channel = event["channel"]
    thread_ts = event["ts"]

    buttons = _build_agent_buttons(channel, thread_ts)

    say(
        text="选择一个 Agent 开始对话：",
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*选择一个 Agent 开始对话：*"},
            },
            {
                "type": "actions",
                "elements": buttons,
            },
        ],
        thread_ts=thread_ts,
    )


# ── 2. 按钮点击 → 弹出 Modal ─────────────────────────────────────────

def _make_button_handler(agent_name: str):
    """为每个 Agent 生成一个按钮点击 handler（闭包）。"""

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
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "message_block",
                        "label": {"type": "plain_text", "text": f"向 {agent_name} 发送消息"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "message_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "输入你的消息..."},
                        },
                    }
                ],
            },
        )

    return handler


def register_agent_actions():
    """为所有已加载的 Agent 注册按钮 action handler。"""
    all_names = ["Leader"] + _TeammateManager.member_names()
    for name in all_names:
        action_id = f"select_agent_{name}"
        app.action(action_id)(_make_button_handler(name))


# ── 3. Modal 提交 → 路由到 Agent ──────────────────────────────────────

@app.view("ask_agent_modal")
def handle_modal_submit(ack, body, client):
    ack()

    meta = body["view"]["private_metadata"]
    agent_name, channel, thread_ts = meta.split("|", 2)
    message = body["view"]["state"]["values"]["message_block"]["message_input"]["value"]
    user_id = body["user"]["id"]

    # 注册输出回调
    _OutputCallbacks[agent_name] = SlackOutputSink(client, channel, thread_ts)

    # 发送确认消息到 Thread
    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"✅ <@{user_id}> → *{agent_name}*: {message}",
    )

    # 路由到 Agent
    _TeammateManager.send_message_to_agent(agent_name, message, f"SlackUser:{user_id}")


# ── 忽略 Bot 自身消息事件 ──────────────────────────────────────────────

@app.event("message")
def handle_message_events(body, logger):
    pass


# ── 启动 ──────────────────────────────────────────────────────────────

def main():
    register_agent_actions()

    # 启动 Leader 消费线程
    leader_thread = threading.Thread(target=_leader_consumer_thread, daemon=True, name="LeaderConsumerThread")
    leader_thread.start()
    print("[SlackAdapter] Leader 消费线程已启动")

    print("[SlackAdapter] Bot 已启动，等待 Slack 事件...")
    handler = SocketModeHandler(app, os.getenv("SLACK_SOCKET_TOKEN"))
    handler.start()


if __name__ == "__main__":
    main()
