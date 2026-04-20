#!/usr/bin/env python3
"""PostToolUse hook — 日志记录工具调用结果。"""
import json
import os
from datetime import datetime

tool_name = os.environ.get("HOOK_TOOL_NAME", "unknown")
tool_output = os.environ.get("HOOK_TOOL_OUTPUT", "")

ts = datetime.now().strftime("%H:%M:%S")
summary = tool_output[:150] if tool_output else "(empty)"
print(f"[{ts}] PostToolUse: {tool_name} -> {summary[:80]}...")

# 如果输出包含 "Error"，注入一条警告消息（退出码 2）
if "Error" in summary:
    print(f"Warning: {tool_name} returned an error", file=sys.stderr)
    exit(2)

exit(0)