#!/usr/bin/env python3
# 框架：可扩展性 —— 在不改动循环的情况下注入行为。
"""
s08_hook_system_CN.py - Hook 系统

Hook 是围绕主循环的扩展点。
它们允许读者在不重写循环本身的情况下添加行为。

教学版本：
  - SessionStart
  - PreToolUse
  - PostToolUse

教学用退出码约定：
  - 0 -> 继续
  - 1 -> 阻止
  - 2 -> 注入一条消息

这刻意比生产系统更简单。这里的目标是
在引入事件特定边界情况之前，先清晰讲解扩展模式。

关键洞察：“不触碰循环也能扩展代理”。
"""

import json
import os
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

# 教学版本只保留最清晰的三个事件。更完整的系统可在后续扩展事件面。

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30  # 秒
# 真实 CC 超时设置：
#   TOOL_HOOK_EXECUTION_TIMEOUT_MS = 600000 (工具 Hook 10 分钟)
#   SESSION_END_HOOK_TIMEOUT_MS = 1500 (SessionEnd Hook 1.5 秒)

# 工作区信任标记。仅当该文件存在（或处于 SDK 模式）才运行 Hook。
TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"


class HookManager:
    """
    从 .hooks.json 配置加载并执行 Hook。

    Hook 管理器只做三件事：
    - 加载 Hook 定义
    - 为事件运行匹配的命令
    - 为调用方汇总阻止/消息结果
    """

    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {"PreToolUse": [], "PostToolUse": [], "SessionStart": []}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                for event in HOOK_EVENTS:
                    self.hooks[event] = config.get("hooks", {}).get(event, [])
                print(f"[已从 {config_path} 加载 Hook]")
            except Exception as e:
                print(f"[Hook 配置错误: {e}]")

    def _check_workspace_trust(self) -> bool:
        """
        检查当前工作区是否受信任。

        教学版本使用一个简单的信任标记文件。
        在 SDK 模式下，信任被视为默认成立。
        """
        if self._sdk_mode:
            return True
        return TRUST_MARKER.exists()

    def run_hooks(self, event: str, context: dict = None) -> dict:
        """
        执行某事件的所有 Hook。

        返回：{"blocked": bool, "messages": list[str]}
          - blocked: 任何 Hook 返回退出码 1 则为 True
          - messages: 退出码 2 的 Hook 的 stderr 内容（用于注入）
        """
        result = {"blocked": False, "messages": []}

        # 信任门：在不受信任的工作区拒绝运行 Hook
        if not self._check_workspace_trust():
            return result

        hooks = self.hooks.get(event, [])

        for hook_def in hooks:
            # 匹配器检查（PreToolUse/PostToolUse 的工具名过滤）
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue

            command = hook_def.get("command", "")
            if not command:
                continue

            # 使用 Hook 上下文构建环境变量
            env = dict(os.environ)
            if context:
                env["HOOK_EVENT"] = event
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False)[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context["tool_output"])[:10000]

            try:
                r = subprocess.run(
                    command, shell=True, cwd=WORKDIR, env=env,
                    capture_output=True, text=True, timeout=HOOK_TIMEOUT,encoding="UTF-8"
                )

                if r.returncode == 0:
                    # 静默继续
                    if r.stdout.strip():
                        print(f"  [hook:{event}] {r.stdout.strip()[:100]}")

                    # 可选的结构化 stdout：一个保持教学约定简单的扩展点
                    try:
                        hook_output = json.loads(r.stdout)
                        if "updatedInput" in hook_output and context:
                            context["tool_input"] = hook_output["updatedInput"]
                        if "additionalContext" in hook_output:
                            result["messages"].append(
                                hook_output["additionalContext"])
                        if "permissionDecision" in hook_output:
                            result["permission_override"] = (
                                hook_output["permissionDecision"])
                    except (json.JSONDecodeError, TypeError):
                        pass  # stdout 不是 JSON —— 对简单 Hook 很正常

                elif r.returncode == 1:
                    # 阻止执行
                    result["blocked"] = True
                    reason = r.stderr.strip() or "被 Hook 阻止"
                    result["block_reason"] = reason
                    print(f"  [hook:{event}] 已阻止: {reason[:200]}")

                elif r.returncode == 2:
                    # 注入消息
                    msg = r.stderr.strip()
                    if msg:
                        result["messages"].append(msg)
                        print(f"  [hook:{event}] 注入: {msg[:200]}")

            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] 超时 ({HOOK_TIMEOUT}s)")
            except Exception as e:
                print(f"  [hook:{event}] 错误: {e}")

        return result


# -- 工具实现（同 s02） --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径越界: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：已阻止危险命令"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：超时 (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误：{e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"错误：未在 {path} 找到要替换的文本"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

TOOLS = [
    {"name": "bash", "description": "运行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入内容到文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

SYSTEM = f"你是位于 {WORKDIR} 的编码代理。使用工具解决任务。"


def agent_loop(messages: list, hooks: HookManager):
    """
    支持 Hook 的代理循环。

    教学版本只保留最清晰的集成点：
    SessionStart, PreToolUse, 执行工具, PostToolUse。
    """
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_input = dict(block.input or {})
            ctx = {"tool_name": block.name, "tool_input": tool_input}

            # -- PreToolUse hooks --
            pre_result = hooks.run_hooks("PreToolUse", ctx)

            # 将 Hook 消息注入到结果中
            for msg in pre_result.get("messages", []):
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"[Hook 消息]: {msg}",
                })

            if pre_result.get("blocked"):
                reason = pre_result.get("block_reason", "被 Hook 阻止")
                output = f"工具被 PreToolUse Hook 阻止: {reason}"
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": output,
                })
                continue

            # -- 执行工具 --
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**tool_input) if handler else f"未知工具: {block.name}"
            except Exception as e:
                output = f"错误：{e}"
            print(f"> {block.name}: {str(output)[:200]}")

            # -- PostToolUse hooks --
            ctx["tool_output"] = output
            post_result = hooks.run_hooks("PostToolUse", ctx)

            # 注入后置 Hook 消息
            for msg in post_result.get("messages", []):
                output += f"\n[Hook 备注]: {msg}"

            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    hooks = HookManager()

    # 触发 SessionStart hooks
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})

    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, hooks)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
