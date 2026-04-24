#!/usr/bin/env python3
# Harness: resilience -- 健壮的智能体应当恢复而不是崩溃。
"""
s11_error_recovery.py - 错误恢复

三条恢复路径的教学演示：

- 输出被截断时继续
- 上下文过大时压缩
- 传输错误为临时时退避重试

    LLM response
         |
         v
    [Check stop_reason]
         |
         +-- "max_tokens" ----> [策略1：max_output_tokens 恢复]
         |                       注入续写消息：
         |                       "Output limit hit. Continue directly."
         |                       最多重试 MAX_RECOVERY_ATTEMPTS (3) 次。
         |                       计数器：max_output_recovery_count
         |
         +-- API error -------> [检查错误类型]
         |                       |
         |                       +-- prompt_too_long --> [策略2：压缩 + 重试]
         |                       |   触发 auto_compact（LLM 摘要）。
         |                       |   用摘要替换历史。
         |                       |   重新执行本轮。
         |                       |
         |                       +-- connection/rate --> [策略3：退避重试]
         |                           指数退避：base * 2^attempt + jitter
         |                           最多重试 3 次。
         |
         +-- "end_turn" -----> [正常结束]

    恢复优先级（先匹配者优先）：
    1. max_tokens -> 注入续写，重试
    2. prompt_too_long -> 压缩，重试
    3. 连接错误 -> 退避，重试
    4. 所有重试耗尽 -> 优雅失败
"""

import json
import os
import random
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic, APIError
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 恢复相关常量
MAX_RECOVERY_ATTEMPTS = 3
BACKOFF_BASE_DELAY = 1.0  # seconds
BACKOFF_MAX_DELAY = 30.0  # seconds
TOKEN_THRESHOLD = 50000   # chars / 4 ~ tokens for compact trigger

CONTINUATION_MESSAGE = (
    "输出达到上限。请直接从中断处继续——"
    "不要总结、不要重复。如有需要，可在句中接续发言。"
)


def estimate_tokens(messages: list) -> int:
    """粗略 token 估计：约 4 字符/1 token。"""
    return len(json.dumps(messages, default=str)) // 4


def auto_compact(messages: list) -> list:
    """
    将会话历史压缩为简短的续写摘要。
    """
    conversation_text = json.dumps(messages, default=str)[:80000]
    prompt = (
        "为保证连续性，请总结这段对话。包含：\n"
        "1) 任务概览与成功标准\n"
        "2) 当前状态：已完成工作、涉及文件\n"
        "3) 关键决策与失败方案\n"
        "4) 剩余下一步\n"
        "请简洁但保留关键信息。\n\n"
        + conversation_text
    )
    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        summary = response.content[0].text
    except Exception as e:
        summary = f"(压缩失败：{e})。之前上下文已丢失。"

    continuation = (
        "本次会话从被压缩的上一段对话继续。"
        f"此前上下文摘要：\n\n{summary}\n\n"
        "请从中断处继续，不要重新向用户提问。"
    )
    return [{"role": "user", "content": continuation}]


def backoff_delay(attempt: int) -> float:
    """带抖动的指数退避：base * 2^attempt + random(0, 1)。"""
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    jitter = random.uniform(0, 1)
    return delay + jitter


# -- 工具实现 --
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

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

SYSTEM = f"你是位于 {WORKDIR} 的编码智能体。使用工具完成任务。"


def agent_loop(messages: list):
    """
    具备错误恢复能力的 agent loop：

    1. max_tokens 后续写继续
    2. prompt-too-long 后压缩再试
    3. 临时传输失败退避再试
    """
    max_output_recovery_count = 0

    while True:
        # -- 带连接重试的 API 调用 --
        response = None
        for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):
            try:
                response = client.messages.create(
                    model=MODEL, system=SYSTEM, messages=messages,
                    tools=TOOLS, max_tokens=200,#更容易触发最大上下文
                )
                break  # success

            except APIError as e:
                error_body = str(e).lower()

                # 策略2：prompt_too_long -> 压缩并重试
                if "overlong_prompt" in error_body or ("prompt" in error_body and "long" in error_body):
                    print(f"[恢复] 提示词过长，正在压缩...（尝试 {attempt + 1}）")
                    messages[:] = auto_compact(messages)
                    continue

                # 策略3：connection/rate 错误 -> 退避重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay = backoff_delay(attempt)
                    print(f"[恢复] API 错误：{e}。"
                          f"{delay:.1f} 秒后重试（尝试 {attempt + 1}/{MAX_RECOVERY_ATTEMPTS}）")
                    time.sleep(delay)
                    continue

                # 所有重试耗尽
                print(f"[错误] API 调用在重试 {MAX_RECOVERY_ATTEMPTS} 次后失败：{e}")
                return

            except (ConnectionError, TimeoutError, OSError) as e:
                # 策略3：网络层错误 -> 退避重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay = backoff_delay(attempt)
                    print(f"[恢复] 连接错误：{e}。"
                          f"{delay:.1f} 秒后重试（尝试 {attempt + 1}/{MAX_RECOVERY_ATTEMPTS}）")
                    time.sleep(delay)
                    continue

                print(f"[错误] 连接在重试 {MAX_RECOVERY_ATTEMPTS} 次后失败：{e}")
                return

        if response is None:
            print("[错误] 未收到响应。")
            return

        messages.append({"role": "assistant", "content": response.content})

        # -- 策略1：max_tokens 恢复 --
        if response.stop_reason == "max_tokens":
            max_output_recovery_count += 1
            if max_output_recovery_count <= MAX_RECOVERY_ATTEMPTS:
                print(f"[恢复] 命中 max_tokens "
                      f"({max_output_recovery_count}/{MAX_RECOVERY_ATTEMPTS})。"
                      "注入续写提示...")
                messages.append({"role": "user", "content": CONTINUATION_MESSAGE})
                continue  # retry the loop
            else:
                print(f"[错误] max_tokens 恢复耗尽 "
                      f"（{MAX_RECOVERY_ATTEMPTS} 次）。停止。")
                return

        # 成功非 max_tokens 响应时重置计数
        max_output_recovery_count = 0

        # -- 正常 end_turn：未请求工具 --
        if response.stop_reason != "tool_use":
            return

        # -- 处理工具调用 --
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**(block.input or {})) if handler else f"未知工具：{block.name}"
            except Exception as e:
                output = f"错误：{e}"
            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})

        # 判断是否需要自动压缩（主动而非被动）
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[恢复] Token 估计超过阈值，自动压缩中...")
            messages[:] = auto_compact(messages)


if __name__ == "__main__":
    print("[错误恢复已启用：max_tokens / prompt_too_long / 连接退避]")
    history = []
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
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
