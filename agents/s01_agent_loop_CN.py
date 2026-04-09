#!/usr/bin/env python3
# Harness: the loop -- keep feeding real tool results back into the model.
"""
s01_agent_loop.py - 代理循环

此文件展示了最小但有用的编码代理模式：

    用户消息
      -> 模型回复
      -> 如果是工具使用：执行工具
      -> 将工具结果写回消息
      -> 继续循环

它有意保持循环小巧，但仍然明确循环状态，
以便后续章节可以在此结构基础上扩展。
"""

import os
import subprocess
from dataclasses import dataclass

try:
    import readline
    # #143 macOS libedit 的 UTF-8 退格修复
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# if os.getenv("ANTHROPIC_BASE_URL"):
#     os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化Anthropic客户端，使用环境变量中的API密钥和基础URL
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"), api_key=os.getenv("ANTHROPIC_API_KEY"))
# 从环境变量获取模型ID
MODEL = os.environ["MODEL_ID"]

# 系统提示，告诉模型它是一个编码代理，位于当前工作目录
SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to inspect and change the workspace. Act first, then report clearly."
)

# 定义工具列表，目前只有bash工具
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command in the current workspace.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


@dataclass
class LoopState:
    # 最小循环状态：历史消息、循环计数和继续循环的原因
    messages: list  # 存储对话历史
    turn_count: int = 1  # 记录对话轮次
    transition_reason: str | None = None  # 记录状态转换的原因


def run_bash(command: str) -> str:
    """
    执行bash命令并返回输出
    
    Args:
        command: 要执行的bash命令
        
    Returns:
        命令执行的输出结果
    """
    # 危险命令列表，防止执行危险操作
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    
    try:
        # 执行命令，捕获标准输出和标准错误
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,  # 设置2分钟超时
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

    # 合并标准输出和标准错误
    output = (result.stdout + result.stderr).strip()
    # 限制输出长度，最多50000字符
    return output[:50000] if output else "(no output)"


def extract_text(content) -> str:
    """
    从消息内容中提取文本
    
    Args:
        content: 消息内容，通常是一个列表
        
    Returns:
        提取出的文本字符串
    """
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def execute_tool_calls(response_content) -> list[dict]:
    """
    执行模型请求的工具调用
    
    Args:
        response_content: 模型的响应内容，包含可能的工具调用
        
    Returns:
        工具执行结果的列表
    """
    results = []
    for block in response_content:
        # 检查是否是工具调用
        if block.type != "tool_use":
            continue
        # 获取命令参数
        command = block.input["command"]
        # 打印命令（黄色）
        print(f"\033[33m$ {command}\033[0m")
        # 执行命令
        output = run_bash(command)
        # 打印命令输出（前200个字符）
        print(output[:200])
        # 构建工具结果
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })
    return results


def run_one_turn(state: LoopState) -> bool:
    """
    运行一轮代理循环
    
    Args:
        state: 循环状态对象
        
    Returns:
        如果需要继续循环返回True，否则返回False
    """
    # 调用Claude模型获取响应
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=state.messages,
        tools=TOOLS,
        max_tokens=8000,
    )
    # 将模型响应添加到消息历史
    state.messages.append({"role": "assistant", "content": response.content})

    # 检查是否需要执行工具调用
    if response.stop_reason != "tool_use":
        state.transition_reason = None
        return False

    # 执行工具调用
    results = execute_tool_calls(response.content)
    if not results:
        state.transition_reason = None
        return False

    # 将工具执行结果添加到消息历史
    state.messages.append({"role": "user", "content": results})
    state.turn_count += 1
    state.transition_reason = "tool_result"
    return True


def agent_loop(state: LoopState) -> None:
    """
    代理循环主函数，持续运行直到不需要继续
    
    Args:
        state: 循环状态对象
    """
    while run_one_turn(state):
        pass


if __name__ == "__main__":
    history = []
    while True:
        try:
            # 读取用户输入（青色提示符）
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        # 检查是否退出
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 将用户输入添加到历史
        history.append({"role": "user", "content": query})
        # 创建循环状态
        state = LoopState(messages=history)
        # 运行代理循环
        agent_loop(state)

        # 提取并打印最终结果
        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
