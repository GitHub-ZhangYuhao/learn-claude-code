#!/usr/bin/env python3
# Harness: tool dispatch -- expanding what the model can reach.
"""
AIAgentIMP.py - Tool dispatch + message normalization

Key insight: "The loop didn't change at all. I just added tools."
"""

import json
import os
import subprocess
import threading
import base64
from pathlib import Path
from queue import Empty, Full

from openai import OpenAI
from dotenv import load_dotenv
from GlobalConfig import *
from SystemPromptBuilder import SystemPromptBuilder
from ErrorRecovery import ErrorRecoveryManager
from TodoManager import TODO_TOOL_SCHEMA, TodoManager
from DefaultToolManager import BASIC_TOOLS, BASIC_TOOL_HANDLERS, compact_history
from TeammateManager import *
from GlobalConfig import _MainAgent_InputQueue, _MainAgent_IdleStatus, _MainAgent_Lock, _MainAgent_HOOKS, _AgentTeam_OutputPrint
from HookManager import *
from SkillManager import SkillRegistry
from SubAgentLoader import SubAgentLoader
from MemoryManager import MEMORY_SAVE_MEMORY_TOOL_HANDLERS, MEMORY_MANAGER_TOOL_SCHEMA
from MCPManager import MCPManager
from ImageToolManager import IMAGE_GENERATION_TOOL, IMAGE_GENERATION_TOOL_HANDLERS, build_vision_feedback_message, extract_saved_paths
import GlobalConfig


#成员初始化

GlobalConfig._TeammateManager = TeammateManager()
_MainAgent_TODO = TodoManager()
_SystemPromptManger = None
_MAIN_AGENT_EXIT = object()
_MainAgent_Skills = SkillRegistry(SKILLS_DIR, ["pdf", "yh-test"])       #加载 skills
_MainAgent_HOOKS = HookManager()

# MCP 管理器初始化
_MCPManager = MCPManager()
_MCPManager.connect_to_servers()


def MainAgentPrint(text: str, msg_type: str = "text"):
    """主 Agent 输出函数，同时推送到外部前端（如 Slack）。"""
    print(f"\033[1m{text}\033[0m")
    _AgentTeam_OutputPrint.put({"agent_name": "Leader", "msg_type": msg_type, "content": text})


'''
Tool Handler
'''
TOOL_HANDLERS = BASIC_TOOL_HANDLERS.copy()
#添加计划工具
TOOL_HANDLERS["todo"]                   = lambda **kw: _MainAgent_TODO.update(kw["items"])
#添加Teammate工具
TOOL_HANDLERS["spawn_teammate"]         = lambda **kw: GlobalConfig._TeammateManager.spawn(kw["name"], kw["role"], kw.get("prompt"))
TOOL_HANDLERS["list_teammates"]         = lambda **kw: GlobalConfig._TeammateManager.list_all()
TOOL_HANDLERS["send_message_to_agent"]  = lambda **kw: GlobalConfig._TeammateManager.send_message_to_agent(kw["agent_name"], kw["prompt"], kw["send_from"])
#添加MemorySave工具
TOOL_HANDLERS.update(MEMORY_SAVE_MEMORY_TOOL_HANDLERS)
#添加图片生成工具
TOOL_HANDLERS.update(IMAGE_GENERATION_TOOL_HANDLERS)
'''
Tool Schema
'''
# 基础工具 + 计划工具 + Teammate工具 + 记忆工具 + 图片生成工具 + MCP工具
TOOLS = (BASIC_TOOLS +
         TODO_TOOL_SCHEMA +
         TEAMMATE_TOOL_SCHEMA +
         SPAWN_AGENT_TOOL_SCHEMA +
         MEMORY_MANAGER_TOOL_SCHEMA +
         IMAGE_GENERATION_TOOL +
         _MCPManager.get_tools_schema())

# 加载 .agent 下的所有子Agent
_SubAgentLoader = SubAgentLoader()
for name, config in _SubAgentLoader.load().items():
    GlobalConfig._TeammateManager.spawn(
                    name=config.name,
                    role=config.description,
                    MCPs=config.MCPs,
                    skills=config.skills,
                    agent_detail=config.detail,
    )
print(GlobalConfig._TeammateManager.list_all())

# 加载主Agent skill
_MainAgent_Skills = SkillRegistry(SKILLS_DIR)
_SystemPromptManger = SystemPromptBuilder(workdir=WORKDIR, tools=TOOLS, skill_registry=_MainAgent_Skills)
#Debug
#test = _SystemPromptManger.build()

def agent_loop(messages: list):
    # --[计划工具]-- 初始化,每次对话都要重新初始化
    _MainAgent_TODO = TodoManager()
    # --[Error Recovery] -- 初始化
    error_recovery_manager = ErrorRecoveryManager()
    # 最大工具调用轮次，防止 LLM 陷入工具循环
    while True:
        # --[压缩历史记录]--
        if len(messages) > CONTEXT_LIMIT:
            MainAgentPrint(f"历史记录长度超过 {CONTEXT_LIMIT}，开始压缩历史记录", "tool_call")
            messages[:] = compact_history(messages)

        # 构建 系统提示词
        messages = _SystemPromptManger.setup_system_prompt(messages)

        #初始化
        response = None

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS[:128],  #OpenAI限制最大工具为128 个
                max_tokens=50000,
            )
            # --[Error Recovery] -- 错误恢复决策错误恢复决策
            recover_decision = error_recovery_manager.choose_recovery(response.choices[0].finish_reason, None)
        except Exception as e:
            # --[Error Recovery] -- 错误恢复决策错误恢复决策
            recover_decision = error_recovery_manager.choose_recovery(None, str(e).lower())

        msg = response.choices[0].message.content
        # OpenAI 模型要求 tool 结果前必须有包含 tool_calls 的完整 assistant 消息
        if any(k in MODEL.lower() for k in ("gpt", "openai", "o1", "o3", "o4")):
            messages.append(response.choices[0].message.to_dict())
        elif msg:
            messages.append({"role": "assistant", "content": msg})
        if msg:
            MainAgentPrint(msg)

        # --[Error Recovery]--错误恢复处理
        has_error, need_continue, messages = error_recovery_manager.recovery_by_decision(recover_decision, messages,attempt=1)
        if has_error:
            if need_continue:
                continue
            else:
                break

        if response.choices[0].finish_reason != "tool_calls":
            return

        # 待回喂的 vision 消息（在所有 tool_calls 处理完后统一追加，避免破坏消息顺序）
        pending_vision_messages = []

        # 遍历所有的 toolcall
        for ToolCall in response.choices[0].message.tool_calls:
            tool_name = ToolCall.function.name
            tool_args = json.loads(ToolCall.function.arguments)
            tool_id = ToolCall.id

            # [HOOK] 添加 PreToolCall Hooks
            hook_ctx = {"tool_name" : tool_name, "tool_input":tool_args}
            pre_tool_hook_result = _MainAgent_HOOKS.run_hooks(HOOK_EVENTS[1], hook_ctx)
            messages = append_hook_result_to_messages(pre_tool_hook_result, tool_id, messages)
            should_block_tool_use, messages =  hook_should_block_tool_use(pre_tool_hook_result, tool_id, messages)
            if should_block_tool_use:
                continue

            # MCP 工具单独分发
            if tool_name in _MCPManager.get_mcp_tool_names():
                output = _MCPManager.call_tool(tool_name, tool_args)
            elif tool_name == "compact_history":    # 压缩历史记录工具, 需要单独处理
                MainAgentPrint("开始压缩历史记录",  "tool_call")
                messages = compact_history(messages)
                MainAgentPrint(f"压缩历史记录结果：{messages}",  "tool_result")
                continue
            else:
                handler = TOOL_HANDLERS.get(tool_name)
                output = handler(**tool_args) if handler else f"Unknow Tool: {tool_name}"
            MainAgentPrint(f"> \n使用工具：{tool_name} : 参数：{tool_args}", "tool_call")
            MainAgentPrint(str(output)[:500], "tool_result")

            # [HOOK] 添加 PostToolCall Hooks
            hook_ctx["tool_output"] = output
            post_hook_result = _MainAgent_HOOKS.run_hooks(HOOK_EVENTS[2], hook_ctx)
            messages = append_hook_result_to_messages(post_hook_result, tool_id, messages)

            # 检查是否是用来 计划 工具
            _MainAgent_TODO.check_used_todo_tool(tool_name)
            # 将 toolcall 添加到 messages 历史中
            result = {"role": "tool", "tool_call_id": ToolCall.id,"content": output}
            messages.append(result)

            # 图片生成工具：收集 vision 回喂消息（循环结束后统一追加）
            if tool_name == "generate_image":
                vision_msg = build_vision_feedback_message(output)
                if vision_msg:
                    pending_vision_messages.append(vision_msg)
                # 推送生成的图片路径到前端（如 Slack），content 为本地文件路径
                for img_path in extract_saved_paths(output):
                    MainAgentPrint(img_path, "image")

        # 所有 tool 结果追加完毕后，再统一追加 vision 回喂消息，让 Agent "看到"生成的图
        for vision_msg in pending_vision_messages:
            messages.append(vision_msg)
            MainAgentPrint("已将生成的图片回喂给 Agent（vision）", "tool_result")

        # 代办工具需要特殊处理，需要在 toolcall 后添加 3 轮的提醒
        messages = _MainAgent_TODO.post_tool_call(messages)





global _MainAgent_InputQueue
global _MainAgent_IdleStatus
global _MainAgent_Lock


def _load_local_image(file_path: str) -> dict:
    """从本地文件路径加载图片，返回 OpenAI vision 格式的 image_url dict。"""
    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"图片文件不存在: {file_path}")
    ext = Path(file_path).suffix.lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(ext, "image/png")
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def enqueue_main_agent_input():
    global _MainAgent_InputQueue
    while True:
        while _MainAgent_InputQueue.empty() and _MainAgent_IdleStatus:
            sleep(2)
            query = input("用户：>>")

            # 支持 !img /path/to/image.png 语法
            images = []
            text = query
            if query.startswith("!img "):
                parts = query.split(None, 2)
                if len(parts) >= 2:
                    img_path = parts[1]
                    text = parts[2] if len(parts) > 2 else "请分析这张图片。"
                    try:
                        images.append(_load_local_image(img_path))
                        print(f"[加载图片] {img_path}")
                    except FileNotFoundError as e:
                        print(f"[错误] {e}")
                        continue

            GlobalConfig._TeammateManager.send_message_to_agent("Leader", text, "User", images=images)

def begin_main_agent_single_loop():
    global _MainAgent_IdleStatus
    global _MainAgent_Lock
    with _MainAgent_Lock:
        _MainAgent_IdleStatus = False

def end_main_agent_single_loop():
    global _MainAgent_IdleStatus
    global _MainAgent_Lock
    with _MainAgent_Lock:
        _MainAgent_IdleStatus = True

def AgentTeamMain(should_use_input_thread:bool = True):
    global _MainAgent_HOOKS
    global _MainAgent_InputQueue

    history = []
    if should_use_input_thread:
        input_thread = threading.Thread(
            target=enqueue_main_agent_input,
            daemon=True,
            name="MainAgentInputThread",
        )
        input_thread.start()

    while True:
        # tick 获取 用户输入
        if not _MainAgent_InputQueue.empty():
            user_query_stream = _MainAgent_InputQueue.get()

            # --[手动触发压缩历史记录]--
            if "/compact" in user_query_stream["content"]:
                MainAgentPrint("手动触发压缩历史记录", "tool_call")
                history = compact_history(history)
                continue


            history.append(user_query_stream)

            # [HOOK] 添加 SessionStart Hook
            _MainAgent_HOOKS.run_hooks(HOOK_EVENTS[0], {"tool_name": "", "tool_input": {}})

            # 修改主Agent状态
            begin_main_agent_single_loop()
            agent_loop(history)
            # 修改主Agent状态
            end_main_agent_single_loop()

            print()

        sleep(2)

if __name__ == "__main__":
    AgentTeamMain()
