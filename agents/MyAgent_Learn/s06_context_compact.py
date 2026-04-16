# Harness： 按需获取知识 —— 低成本发掘技能，仅在需要时加载相关内容。
"""
s05_skill_loading.py - 技能加载

本章介绍双层技能模型：

1. 在系统提示中放入一个轻量级技能目录。
2. 仅当模型请求时才加载完整的技能内容。

这样可以保持提示词小巧，同时让模型能够访问可重用的、特定任务的指导。
"""

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import json
from anthropic import Anthropic
from dotenv import load_dotenv

import yaml # 导入PyYAML库，用于解析markdown前置内容

from agents.s06_context_compact import PERSIST_THRESHOLD

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    f"你是位于 {WORKDIR} 的编码 agent。"
    "请逐步持续工作，当对话过长时使用压缩功能。"
)

CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
PREVIEW_CHARS = 2000
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task outputs" / "tool-results"


"""
Context Compact class
"""
@dataclass
class CompactState:
    """压缩状态记录"""
    has_compacted: bool = False
    last_summary: str = ""
    recent_files: list[str] = field(default_factory=list)
def estimate_context_size(message: list) -> int:
    """估算上下文长度"""
    return len(str(message))

#这需要再读取文件的时候调用
def track_recent_file(state: CompactState, path: str) -> None:
    """跟踪最近访问的文件"""
    if path in state.recent_files:
        state.recent_files.remove(path)
    state.recent_files.append(path)
    if len(state.recent_files) > 5:
        state.recent_files[:] = state.recent_files[-5:]     #只保留5个最近文件

# 安全路径解析函数,确保只在安全工作区内操作
def safe_path(path_str: str) -> Path:
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return path

def persist_large_output(tool_use_id: str, output:str) -> str:
    """持久化大型输出到磁盘"""
    if len(output) <= PERSIST_THRESHOLD:
        return output

    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not stored_path.exists():
        stored_path.write_text(output)

    preview = output[:PREVIEW_CHARS]
    rel_path = stored_path.relative_to(WORKDIR)
    return (
        "<persisted-output>\n"
        f"完整的输出已保存到：{rel_path}"
        "预览： \n"
        f"{preview}\n"
        "</persisted-output>"
    )

# 将messages中的tool_result对应的输出收集起来
def collect_tool_result_blocks(messages: list) -> list[tuple[int, int, dict]]:
    """收集messages中的tool_result对应的输出"""
    block = []
    # 遍历messages
    for message_index,message in enumerate(messages):
        content = message.get("content")
        # 工具调用再user输出中，如果不是user输出，或者不是不是列表，跳过
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        #遍历user的信息块，如果是tool_result，收集起来
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block.append((message_index, block_index, block))
    return block

def micro_compact(message: list) -> list:
    """微压缩：将旧的工具结果压缩为占位符"""
    tool_results = collect_tool_result_blocks(message)
    # 如果总共的工具调用小于3次， 就保留，否则进行工具调用的压缩
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return message
    """
    切片操作：[:-KEEP_RECENT_TOOL_RESULTS] 是 Python 的切片语法，-KEEP_RECENT_TOOL_RESULTS 表示从末尾开始计数
    假设：
    KEEP_RECENT_TOOL_RESULTS = 2
    tool_results 包含 5 个工具结果：[result1, result2, result3, result4, result5]
    处理过程：
    tool_results[:-2] → 获取前 3 个旧结果：[result1, result2, result3]
    遍历这 3 个旧结果，将长内容替换为占位符
    保留最近的 2 个结果：result4, result5 不变
    """
    # 处理旧的工具结果 假设工具结果有 5个，那么处理 （5-KEEP_RECENT_TOOL_RESULTS） 个结果
    for _,_,block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content = block.get("content", "")
        #只处理内容超过120个字符的工具结果
        if not isinstance(content, str) or len(content) <= 120:
            continue
        block["content"] = "[早期工具结果已压缩。如需完整详情请重新运行工具。]"
    return message

#将历史对话写入脚本 , 返回写入文件的路径
def write_transcript(messages: list) -> Path:
    """将对话写入记录文件"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, default=str) + "\n")
    return path

# 调用大模型对历史对话进行摘要 , 返回摘要的文本
def summarize_history(message: list) -> str:
    conversation = json.dumps(message, default=str)[:80000]  #字符串切片操作，只保留前 80000 个字符
    prompt = (
        "请总结这段 agent 的对话， 以便继续工作。 \n"
        "请保留：\n"
        "1. 当前目标\n"
        "2. 重要发现和决策\n"
        "3. 已读取或修改的文件\n"
        "4. 剩余工作\n"
        "5. 用户的约束和偏好\n"
        "请简洁但具体。 \n\n"
        f"\n{conversation}"
           )
    response = client.messages.create(
        model = MODEL,
        messages = [{"role":"user", "content": prompt}],
        max_tokens = 2000
    )
    return response.content[0].text.strip()

"""
压缩历史对话为摘要 , 这里的focus是可以选择，focus,是由 LLM 的工具调用生成的
"""
def compact_history(messages: list, state: CompactState, focus: str | None = None) -> list:
    """压缩历史对话为摘要"""
    # 先将历史对话写入到外部文件中
    transcript_path = write_transcript(messages)
    print(f"[记录已保存到：{transcript_path}]")

    # 调用大模型对历史对话进行摘要
    summary = summarize_history(messages)
    if focus:
        summary += f"\n\n下一步需要保留的焦点: {focus}"
    if state.recent_files:
        recent_lines = "\n".join(f"- {path}" for path in state.recent_files)
        summary += f"\n\n如需可以重新打开的文件：\n{recent_lines}"

    state.has_compacted = True
    state.last_summary = summary

    return[{
        "role": "user",
        "content": (
            "对话已压缩， agent 可以继续工作。 \n\n"
            f"{summary}"
        )
    }]

'''
添加工具函数
'''
def run_bash(command: str, tool_use_id: str) -> str:
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

    output = (result.stdout + result.stderr).strip() or "无输出"
    return persist_large_output(tool_use_id, output)

def run_read(path: str,tool_use_id: str, state: CompactState, limit: int | None = None) -> str:
    try:
        track_recent_file(state, path)
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        output =  "\n".join(lines)[:50000]
        return persist_large_output(tool_use_id, output)
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
Tool Schema
用于给模型描述工具的输入参数和输出结果
'''
TOOLS = [
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
    {
        "name": "compact",
        "description": "摘要早期对话，以便在更小的上下文继续工作。",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus":{"type":"string"}
            },
        }
    },

]

"""
工具调用处理函数
"""
def execute_tool(block, state: CompactState) -> str:
    """执行工具调用"""
    if block.name == "bash":
        return run_bash(block.input["command"], block.id)
    if block.name == "read_file":
        return run_read(block.input["path"], block.id, state, block.input.get("limit"))
    if block.name == "write_file":
        return run_write(block.input["path"], block.input["content"])
    if block.name == "edit_file":
        return run_edit(block.input["path"], block.input["old_text"], block.input["new_text"])
    if block.name == "compact":
        return "正在压缩对话..."
    return f"位置工具: {block.name}"

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

def agent_loop(messages: list, state: CompactState) -> None:
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role":"assistant", "content":response.content })

        # 如果大模型不再调用工具了，就结束循环
        if response.stop_reason != "tool_use":
            return

        result = []
        manual_compact = False
        for block in response.content :
            if block.type != "tool_use":   #在一次回复中有多个block，例如 think block，text block，tool_call block，如果不是tool_call block，就跳过
                continue
            # 如果工具调用是 task 处理 SubAgent， 否则调用普通工具

            output = execute_tool(block, state)
            if block.name == "compact":
                manual_compact = True
                compact_focus = (block.input or {}).get("focus")

            print(f"> {block.name}: {str(output)[:200]}")
            result.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

        #将工具使用的结果通过 role: user ，加入到消息列表中
        messages.append({"role": "user", "content": result})

        if manual_compact:
            print("[手动压缩]")
            messages[:] = compact_history(messages, state, focus=compact_focus)

if __name__ == "__main__":
    history = []
    compact_state = CompactState()

    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "quit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history, compact_state)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
