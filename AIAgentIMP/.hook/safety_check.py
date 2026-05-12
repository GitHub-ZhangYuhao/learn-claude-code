#!/usr/bin/env python3
"""PreToolUse hook — 拦截危险的 bash 命令。"""
import json
import os
import sys

tool_name = os.environ.get("HOOK_TOOL_NAME", "")
tool_input_raw = os.environ.get("HOOK_TOOL_INPUT", "{}")

try:
    tool_input = json.loads(tool_input_raw)
except json.JSONDecodeError:
    sys.exit(0)

command = tool_input.get("command", "")

DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf /*",
    "sudo rm",
    ":(){:|:&};:",  # fork bomb
    "> /dev/sda",
    "dd if=",
    "mkfs",
    "chmod 777 /",
]

# debug 调试
sys.exit(1)


for pattern in DANGEROUS_PATTERNS:
    if pattern in command.lower():
        print(f"BLOCKED dangerous command pattern: {pattern}", file=sys.stderr)
        sys.exit(1)  # 退出码 1 = 阻止执行

# 放行
sys.exit(0)
