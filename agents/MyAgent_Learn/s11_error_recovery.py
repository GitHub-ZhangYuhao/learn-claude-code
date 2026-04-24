# Harness: assembly -- 系统提示词是一个流水线，而非一个字符串。
"""
s10_system_prompt_CN.py - 系统提示词构建

本章讲授一个核心理念：
系统提示词应该由清晰的段落拼装而成，而不是写成一段巨大的硬编码文本。

教学流水线：
  1. 核心指令
  2. 工具列表
  3. 技能元数据
  4. 记忆段落
  5. CLAUDE.md 链
  6. 动态上下文

构建器将稳定信息与经常变化的信息分开存放。
一个简单的 DYNAMIC_BOUNDARY 标记使这种分割一目了然。

每轮次的提醒更加动态。它们更适合作为独立的 user-role 系统提醒注入，
而不是盲目地混入稳定提示词中。

核心洞察："提示词构建是一个带有边界的流水线，而非一个大字符串。"
"""
import datetime
import json
import os
import random
import platform
import re
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from anthropic import Anthropic, APIError

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
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
        result = subprocess.run(command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120, encoding="utf-8"
        )
        out = (result.stdout + result.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="UTF-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        output =  "\n".join(lines)[:50000]
        return output
    except Exception as exc:
        return f"Error: {exc}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="UTF-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        content = file_path.read_text(encoding="UTF-8")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        file_path.write_text(content.replace(old_text, new_text, 1), encoding="UTF-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


"""
工具调用处理函数
"""
TOOL_HANDLERS = {
    "bash":         lambda **kw : run_bash(kw["command"]),
    "read_file":    lambda **kw : run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw : run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw : run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

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
]

SYSTEM = f"你是位于 {WORKDIR} 的编码智能体。使用工具完成任务。"

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
    """
    具备错误恢复能力的 agent loop：

    1. max_tokens 后续写继续
    2. prompt-too-long 后压缩再试
    3. 临时传输失败退避再试
    """
    max_output_recovery_count = 0
    while True:
        response = None
        for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):
            try:
                response = client.messages.create(
                    model=MODEL,
                    system=SYSTEM,
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=200, #更容易触发最大上下文
                )
                break
            except APIError as e:
                # TODO:策略2（prompt too long） 和 策略3（connection/rate错误）
                # 策略2（prompt too long）
                error_body = str(e).lower()
                if "overlong_prompt" in error_body or ("prompt" in error_body and "long" in error_body):
                    print(f"[恢复] 提示词过长， 正在压缩（尝试{attempt + 1}）")
                    messages[:] = auto_compact(messages)
                    continue

                # 策略3：connection/rate 错误 -> 退避重试
                if attempt < MAX_RECOVERY_ATTEMPTS:
                    delay = backoff_delay(attempt)
                    print(f"[恢复] API 错误：{e}"
                          f"{delay:0.1f} 秒后重试（尝试 {attempt + 1}/{MAX_RECOVERY_ATTEMPTS}）")
                    time.sleep(delay)
                    continue

                # 所有重试耗尽
                print(f"[错误] API 调用在重试{MAX_RECOVERY_ATTEMPTS} 次后失败：{e}")
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

        messages.append({"role":"assistant", "content":response.content })


        # -- 策略1：max_token 触发恢复 --
        if response.stop_reason == "max_tokens":
            max_output_recovery_count += 1
            if max_output_recovery_count <= MAX_RECOVERY_ATTEMPTS:
                print(f"[恢复] 命中 max_tokens "
                      f"{max_output_recovery_count} / {MAX_RECOVERY_ATTEMPTS}"
                      "注入续写提示...")
                messages.append({"role":"user", "content":{CONTINUATION_MESSAGE}})
                continue
            else:
                print(f"[错误] max_tokens 恢复次数耗尽"
                      f"({MAX_RECOVERY_ATTEMPTS}次)。停止。")
                return

        # -- 正常 end_turn: 未请求工具 --
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


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
