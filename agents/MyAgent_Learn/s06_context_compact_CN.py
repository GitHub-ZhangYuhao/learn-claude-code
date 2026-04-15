#!/usr/bin/env python3
# Harness: compression -- 保持活跃上下文足够小，以便持续工作。
"""
s06_context_compact_CN.py - 上下文压缩（中文版）

本教学版本有意保持上下文压缩机制简洁明了：

1. 大型工具输出会被持久化到磁盘，并替换为预览标记。
2. 较早的工具结果会被微压缩为简短占位符。
3. 当整个对话过大时，agent 会对其进行摘要，
   然后从该摘要继续工作。

目标不是模拟每一个生产环境的分支，而是让
活跃上下文的概念变得明确且可教学。
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

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


@dataclass
class CompactState:
    """压缩状态记录"""
    has_compacted: bool = False
    last_summary: str = ""
    recent_files: list[str] = field(default_factory=list)


def estimate_context_size(messages: list) -> int:
    """估算上下文大小"""
    return len(str(messages))


def track_recent_file(state: CompactState, path: str) -> None:
    """跟踪最近访问的文件"""
    if path in state.recent_files:
        state.recent_files.remove(path)
    state.recent_files.append(path)
    if len(state.recent_files) > 5:
        state.recent_files[:] = state.recent_files[-5:]


def safe_path(path_str: str) -> Path:
    """确保路径在 workspace 范围内"""
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出 workspace 范围: {path_str}")
    return path


def persist_large_output(tool_use_id: str, output: str) -> str:
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
        f"完整输出已保存到: {rel_path}\n"
        "预览:\n"
        f"{preview}\n"
        "</persisted-output>"
    )


def collect_tool_result_blocks(messages: list) -> list[tuple[int, int, dict]]:
    """收集所有工具结果块"""
    blocks = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((message_index, block_index, block))
    return blocks


def micro_compact(messages: list) -> list:
    """微压缩：将旧的工具结果替换为占位符"""
    tool_results = collect_tool_result_blocks(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages

    for _, _, block in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content = block.get("content", "")
        if not isinstance(content, str) or len(content) <= 120:
            continue
        block["content"] = "[早期工具结果已压缩。如需完整详情请重新运行工具。]"
    return messages


def write_transcript(messages: list) -> Path:
    """将对话写入记录文件"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w") as handle:
        for message in messages:
            handle.write(json.dumps(message, default=str) + "\n")
    return path


def summarize_history(messages: list) -> str:
    """调用模型对历史对话进行摘要"""
    conversation = json.dumps(messages, default=str)[:80000]
    prompt = (
        "请总结这段编码 agent 的对话，以便继续工作。\n"
        "请保留：\n"
        "1. 当前目标\n"
        "2. 重要发现和决策\n"
        "3. 已读取或修改的文件\n"
        "4. 剩余工作\n"
        "5. 用户的约束和偏好\n"
        "请简洁但具体。\n\n"
        f"{conversation}"
    )
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return response.content[0].text.strip()


def compact_history(messages: list, state: CompactState, focus: str | None = None) -> list:
    """压缩历史记录为摘要"""
    transcript_path = write_transcript(messages)
    print(f"[记录已保存: {transcript_path}]")

    summary = summarize_history(messages)
    if focus:
        summary += f"\n\n下一步需要保留的焦点: {focus}"
    if state.recent_files:
        recent_lines = "\n".join(f"- {path}" for path in state.recent_files)
        summary += f"\n\n如需可重新打开的文件:\n{recent_lines}"

    state.has_compacted = True
    state.last_summary = summary

    return [{
        "role": "user",
        "content": (
            "对话已压缩，agent 可以继续工作。\n\n"
            f"{summary}"
        ),
    }]


def run_bash(command: str, tool_use_id: str) -> str:
    """执行 bash 命令"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "错误: 危险命令已被拦截"
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
        return "错误: 超时 (120秒)"

    output = (result.stdout + result.stderr).strip() or "(无输出)"
    return persist_large_output(tool_use_id, output)


def run_read(path: str, tool_use_id: str, state: CompactState, limit: int | None = None) -> str:
    """读取文件内容"""
    try:
        track_recent_file(state, path)
        lines = safe_path(path).read_text(encoding='utf-8').splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        output = "\n".join(lines)
        return persist_large_output(tool_use_id, output)
    except Exception as exc:
        return f"错误: {exc}"


def run_write(path: str, content: str) -> str:
    """写入文件"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as exc:
        return f"错误: {exc}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件中的文本"""
    try:
        file_path = safe_path(path)
        content = file_path.read_text()
        if old_text not in content:
            return f"错误: 在 {path} 中未找到该文本"
        file_path.write_text(content.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as exc:
        return f"错误: {exc}"


TOOLS = [
    {
        "name": "bash",
        "description": "运行 shell 命令。",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文件内容。",
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
        "description": "将内容写入文件。",
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
        "description": "一次性替换文件中的文本。",
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
        "description": "摘要早期对话，以便在更小的上下文中继续工作。",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string"},
            },
        },
    },
]


def extract_text(content) -> str:
    """从响应内容中提取文本"""
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


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
    return f"未知工具: {block.name}"


def agent_loop(messages: list, state: CompactState) -> None:
    """Agent 主循环"""
    while True:
        messages[:] = micro_compact(messages)

        if estimate_context_size(messages) > CONTEXT_LIMIT:
            print("[自动压缩]")
            messages[:] = compact_history(messages, state)

        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        manual_compact = False
        compact_focus = None
        for block in response.content:
            if block.type != "tool_use":
                continue

            output = execute_tool(block, state)
            if block.name == "compact":
                manual_compact = True
                compact_focus = (block.input or {}).get("focus")

            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})

        if manual_compact:
            print("[手动压缩]")
            messages[:] = compact_history(messages, state, focus=compact_focus)


if __name__ == "__main__":
    history = []
    compact_state = CompactState()

    while True:
        try:
            query = input("\033[36ms06_CN >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history, compact_state)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
