# Harness： 上下文隔离 -- 保护模型的思维清晰度
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
#from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from openai import fine_tuning

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PLAN_REMINDER_INTERVAL = 3

SYSTEM = (f"你是一个Coding Agent {WORKDIR}. 使用任务工具来分配探索任务或子任务."
          f"当你认为一个任务比较复杂时候，优先使用任务工具来分配子任务.")
SUBAGENT_SYSTEM = f"你是一个 SubCoding Agent {WORKDIR}. 完成给定的任务，然后总结你的发现结果."

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

    """
    解析代理定义文件。
    例如：
        ---
        name: 代码助手
        model: claude-3-opus-20240229
        tools: bash, read_file
        ---
        你是一个专业的代码助手，擅长解决各种编程问题。
        请分析用户的问题并提供详细的解决方案。
        
    解析为：
        self.name → "代码助手"
        self.config → {"name": "代码助手", "model": "claude-3-opus-20240229", "tools": "bash, read_file"}
        self.system_prompt → "你是一个专业的代码助手，擅长解决各种编程问题。\n请分析用户的问题并提供详细的解决方案。"
    """
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

# 安全路径解析函数,确保只在安全工作区内操作
def safe_path(path_str: str) -> Path:
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return path

'''
添加工具函数
'''
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as exc:
        return f"Error: {exc}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        content = file_path.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        file_path.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"

'''
Tool Handler
**kw：表示接收任意数量的关键字参数
提取可选的limit参数（使用kw.get("limit")，如果不存在则返回None）
'''
TOOL_HANDLERS = {
    "bash" :        lambda **kw: run_bash(kw["command"]),
    "read_file":    lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

'''
Tool Schema
用于给模型描述工具的输入参数和输出结果
'''
CHILD_TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]

# -- 子代理：全新上下文，过滤工具，仅返回摘要 --
def run_subagent(prompt: str) -> str:
    sub_message = [{"role":"user", "content":prompt}]
    for _ in range(30): #安全限制最大调用30次
        response = client.messages.create(
            model       = MODEL,
            system      = SUBAGENT_SYSTEM,
            messages    = sub_message,
            tools       = CHILD_TOOLS,
            max_tokens  =8000,
        )
        # 拼接子代理的回复
        sub_message.append({"role":"assistant", "content":response.content })
        # 如果子代理不需要工具调用了，就结束循环
        if response.stop_reason != "tool_use":
            break
        # 处理工具调用结果
        result = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                result.append({"role":"tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
        # 将工具调用的结果拼接到 sub_messages 里面去
        sub_message.append({"role":"user", "content":result})

    """
    # 1. 收集所有带有 text 属性的内容
    texts = []
    for b in response.content:
        if hasattr(b, "text"):  # 检查对象是否有 text 属性
            texts.append(b.text)  # 如果有，添加到列表中
    
    # 2. 将收集到的文本连接成一个字符串
    result = "".join(texts)
    
    # 3. 检查结果是否为空，如果为空则返回默认值
    if result:
        return result
    else:
        return "(no summary)"
    """
    return "".join([b.text for b in response.content if hasattr(b, "text")]) or "(no summary)"

# -- 父代理工具：基础工具 + 任务分发器 --
PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
        "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        "input_schema":
        {
            "type": "object",
            "properties": { "prompt": {"type": "string"},
                            "description": {"type": "string", "description": "Short description of the task"}},
            "required": ["prompt"], # 必须提供 prompt
        },
    },
]


# 用于提取大模型的最后的输出
def extract_text(content) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()

def agent_loop(messages: list) -> None:
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )
        messages.append({"role":"assistant", "content":response.content })

        # 如果大模型不再调用工具了，就结束循环
        if response.stop_reason != "tool_use":
            return

        result = []
        for block in response.content :
            if block.type != "tool_use":   #在一次回复中有多个block，例如 think block，text block，tool_call block，如果不是tool_call block，就跳过
                continue
            # 如果工具调用是 task 处理 SubAgent， 否则调用普通工具
            if block.name == "task":
                desc = block.input.get("description", "subtask")
                prompt = block.input.get("prompt", "")
                print(f"> task({desc}): {prompt[:80]}")
                output = run_subagent(prompt)
            else:
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            # 拼接工具返回结果块，后续需用凭借进 role: user 中
            print(f"> {block.name} : {str(output)[:200]}")
            result.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

        #将工具使用的结果通过 role: user ，加入到消息列表中
        messages.append({"role": "user", "content": result})

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "quit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
