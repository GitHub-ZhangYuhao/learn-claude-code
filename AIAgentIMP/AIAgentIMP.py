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

load_dotenv(override=True)

#if os.getenv("OPENAI_BASE_URL"):
#    os.environ.pop("OPENAI_API_KEY", None)

# WORKDIR = Path.cwd()
# client = OpenAI(base_url=os.getenv("OPENAI_BASE_URL"), api_key=os.getenv("OPENAI_API_KEY"))
# MODEL = os.environ["MODEL_ID"]

SystemPromptManger = SystemPromptBuilder()

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"



# -- The dispatch map: {tool_name: handler} --
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write content to file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace exact text in file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}
    }},
]


def agent_loop(messages: list):
    while True:
        # 构建 系统提示词
        SystemPrompt = SystemPromptManger.build()
        messages.insert(0, {"role": "system", "content":SystemPrompt})

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )

        msg = response.choices[0].message.content
        if msg !="":
            messages.append({"role": "assistant", "content": msg})
            print(msg)
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
            # 将 toolcall 添加到 messages 历史中
            result = {"role": "tool", "tool_call_id": ToolCall.id,"content": output}
            messages.append(result)




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
