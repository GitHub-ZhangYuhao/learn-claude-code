#!/usr/bin/env python3
"""PreToolUse hook — 拦截危险的 bash 命令。"""
import json
import os
import sys
from os import error

tool_name = os.environ.get("HOOK_TOOL_NAME", "")
tool_input_raw = os.environ.get("HOOK_TOOL_INPUT", "{}")
tool_event = os.environ.get("HOOK_EVENT","")

try:
    tool_input = json.loads(tool_input_raw)
except json.JSONDecodeError:
    sys.exit(0)

command = tool_input.get("command", "")

# 写入 messages 到 hook
sys.stderr.write("My Hook Pre Tool Hook , 当执行到这里的时候说明我的 Hook Work 了")
sys.stdout.write("My Hook Pre Tool Hook , 当执行到这里的时候说明我的 Hook Work 了")

# 放行 并 注入
sys.exit(2)
