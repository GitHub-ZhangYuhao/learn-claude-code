#!/usr/bin/env python3
# Harness: context isolation -- protecting the model's clarity of thought.
"""
s04_subagent.py - 子代理

使用全新的 messages=[] 生成子代理。子代理在自己的上下文中工作，
共享文件系统，然后只向父代理返回摘要。

    父代理                           子代理
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- 全新
    |                  |  分发任务    |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  返回摘要    |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    父代理上下文保持干净。
    子代理上下文被丢弃。

关键洞察："全新的 messages=[] 提供了上下文隔离。父代理保持干净。"

注意：真实的 Claude Code 还使用进程内隔离（非操作系统级进程分叉）。
子代理在同一进程中运行，具有全新的消息数组和隔离的工具上下文——与本教学实现相同的模式。

    与真实 Claude Code 的比较：
    +-------------------+------------------+----------------------------------+
    | 方面              | 本演示版         | 真实 Claude Code                 |
    +-------------------+------------------+----------------------------------+
    | 后端              | 仅进程内         | 5 种后端：进程内、tmux、iTerm2、  |
    |                   |                  | 分叉、远程                       |
    | 上下文隔离        | 全新的 messages=[]| createSubagentContext() 隔离约   |
    |                   |                  | 20 个字段（工具、权限、工作目录、  |
    |                   |                  | 环境、钩子等）                    |
    | 工具过滤          | 手动管理         | resolveAgentTools() 从父代理池   |
    |                   |                  | 过滤；allowedTools 替换所有允许  |
    |                   |                  | 规则                             |
    | 代理定义          | 硬编码系统提示   | .claude/agents/*.md 带有 YAML    |
    |                   |                  | 前置内容（AgentTemplate）         |
    +-------------------+------------------+----------------------------------+

"""

import os
import re
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


class AgentTemplate:
    """
    从markdown前置内容解析代理定义。

    真实的Claude Code从.claude/agents/*.md加载代理定义。
    前置内容字段：name（名称）、tools（工具）、disallowedTools（禁用工具）、skills（技能）、hooks（钩子）、
    model（模型）、effort（努力程度）、permissionMode（权限模式）、maxTurns（最大轮次）、memory（记忆）、isolation（隔离）、color（颜色）、
    background（背景）、initialPrompt（初始提示）、mcpServers（MCP服务器）。
    3个来源：内置、自定义（.claude/agents/）、插件提供。
    """
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.config = {}
        self.system_prompt = ""
        self._parse()

    def _parse(self):
        text = self.path.read_text()
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            self.system_prompt = text
            return
        for line in match.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                self.config[k.strip()] = v.strip()
        self.system_prompt = match.group(2).strip()
        self.name = self.config.get("name", self.name)


# -- 父代理和子代理共享的工具实现 --
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
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

# 子代理获得所有基础工具，除了task（不允许递归生成）
CHILD_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]


# -- 子代理：全新上下文，过滤工具，仅返回摘要 --
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]  # 全新上下文
    for _ in range(30):  # 安全限制
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        sub_messages.append({"role": "user", "content": results})
    # 只有最终文本返回给父代理——子代理上下文被丢弃
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"


# -- 父代理工具：基础工具 + 任务分发器 --
PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
         "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
         "input_schema":
         {
             "type": "object",
             "properties": {"prompt": {"type": "string"},
                            "description": {"type": "string", "description": "Short description of the task"}},
             "required": ["prompt"]
        }
     },
]


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    desc = block.input.get("description", "subtask")
                    prompt = block.input.get("prompt", "")
                    print(f"> task ({desc}): {prompt[:80]}")
                    output = run_subagent(prompt)
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                print(f"  {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()