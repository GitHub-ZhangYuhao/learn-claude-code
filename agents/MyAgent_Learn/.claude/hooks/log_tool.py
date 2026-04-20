#!/usr/bin/env python3
"""PreToolUse hook — 日志记录每次工具调用。"""
import json
import os
from datetime import datetime

tool_name = os.environ.get("HOOK_TOOL_NAME", "unknown")
tool_input_raw = os.environ.get("HOOK_TOOL_INPUT", "{}")

try:
    tool_input = json.loads(tool_input_raw)
except json.JSONDecodeError:
    tool_input = {}

ts = datetime.now().strftime("%H:%M:%S")
summary = json.dumps(tool_input, ensure_ascii=False)[:100]
print(f"[{ts}] PreToolUse: {tool_name} -> {summary}")

# 退出码 0 = 继续
exit(0)