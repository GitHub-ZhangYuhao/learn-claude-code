#!/usr/bin/env python3
# Harness: tool dispatch -- expanding what the model can reach.
"""
AIAgentIMP.py - Tool dispatch + message normalization

Key insight: "The loop didn't change at all. I just added tools."
"""

import json
import os
import subprocess
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv
from GlobalConfig import *
from SystemPromptBuilder import SystemPromptBuilder
from ErrorRecovery import ErrorRecoveryManager
from TodoManager import TODO_TOOL_SCHEMA, TodoManager
from DefaultToolManager import BASIC_TOOLS, BASIC_TOOL_HANDLERS

load_dotenv(override=True)

#if os.getenv("OPENAI_BASE_URL"):
#    os.environ.pop("OPENAI_API_KEY", None)

# WORKDIR = Path.cwd()
# client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.getenv("OPENAI_API_KEY"))
# MODEL = os.environ["MODEL_ID"]

SystemPromptManger = SystemPromptBuilder()




MainAgent_TODO = TodoManager()
'''
Tool Handler
**kw：表示接收任意数量的关键字参数
提取可选的limit参数（使用kw.get("limit")，如果不存在则返回None）
'''

#添加TODO工具
TOOL_HANDLERS = BASIC_TOOL_HANDLERS.copy()
TOOL_HANDLERS["todo"] = lambda **kw: MainAgent_TODO.update(kw["items"])

'''
Tool Schema
用于给模型描述工具的输入参数和输出结果
'''
# 添加 代办 工具描述
TOOLS = BASIC_TOOLS + TODO_TOOL_SCHEMA


def agent_loop(messages: list):
    # --[计划工具]-- 初始化,每次对话都要重新初始化
    MainAgent_TODO = TodoManager()
    # --[Error Recovery] -- 初始化
    error_recovery_manager = ErrorRecoveryManager()
    while True:
        # 构建 系统提示词
        messages = SystemPromptManger.setup_system_prompt(messages)

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                max_tokens=1000,
            )
            # --[Error Recovery] -- 错误恢复决策错误恢复决策
            recover_decision = error_recovery_manager.choose_recovery(response.choices[0].finish_reason, None)
        except Exception as e:
            # --[Error Recovery] -- 错误恢复决策错误恢复决策
            recover_decision = error_recovery_manager.choose_recovery(None, str(e).lower())

        msg = response.choices[0].message.content
        if msg !="":
            messages.append({"role": "assistant", "content": msg})
            print(msg)

        # --[Error Recovery]--错误恢复处理
        has_error, need_continue, messages = error_recovery_manager.recovery_by_decision(recover_decision, messages,attempt=1)
        if has_error:
            if need_continue:
                continue
            else:
                break

        if response.choices[0].finish_reason != "tool_calls":
            return

        # 遍历所有的 toolcall
        for ToolCall in response.choices[0].message.tool_calls:
            tool_name = ToolCall.function.name
            tool_args = json.loads(ToolCall.function.arguments)
            handler = TOOL_HANDLERS.get(tool_name)
            output =  handler(**tool_args) if handler else f"Unknow Tool: {tool_name}"
            print(f"> \n使用工具：{tool_name} : 参数：{tool_args}")
            print(output[:200])
            # 检查是否是用来 计划 工具
            MainAgent_TODO.check_used_todo_tool(tool_name)
            # 将 toolcall 添加到 messages 历史中
            result = {"role": "tool", "tool_call_id": ToolCall.id,"content": output}
            messages.append(result)

        # 代办工具需要特殊处理，需要在 toolcall 后添加 3 轮的提醒
        messages = MainAgent_TODO.post_tool_call(messages)




if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()
