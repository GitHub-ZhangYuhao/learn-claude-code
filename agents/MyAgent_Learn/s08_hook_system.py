# Harness：可扩展性 —— 在不改动循环的情况下注入行为。
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

# 初始化模型和工作区
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 教学版本只保留最清晰的三个事件。更完整的系统可在后续扩展事件面。
HOOK_EVNETS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30 # 秒

#工作区信任标记。仅当文件存在（或处于 SDK 模式 ）才能运行 HOOK。
TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"

class HookManager:
    """
    从 .hooks.json 配置加载并执行 Hook。

    Hook 管理器只做三件事：
    - 加载 Hook 定义
    - 为事件运行匹配的命令
    - 为调用方汇总阻止/消息结果
    """

    def __init__(self, config_path = None, sdk_mode:bool = False):
        self.hooks = {"PreToolUse":[], "PostToolUse":[], "SessionStart":[]}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
                for event in HOOK_EVNETS:
                    self.hooks[event] = config.get("hooks",{}).get(event,[])
                print(f"[已从 {config_path}] 加载 Hook]")
            except Exception as e:
                print(f"[Hook 配置错误：{e}]")

    def _check_workspace_trust(self)-> bool:
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
        result = {"blocked":False, "messages":[]}

        # 信任门：在不受信任的工作区拒绝执行 Hook
        if not self._check_workspace_trust():
            return result

        hooks = self.hooks.get(event,[])

        #遍历当前event的每个hook
        for hook_def in hooks:
            # 匹配器检查（PreToolUse/PostToolUse 的工具名过滤）
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name","")
                # 如果工具名称不为 * 并且匹配不上 tool_name ,那么跳过这个 hook_def
                if matcher != "*" and matcher != tool_name:
                    continue

            # 如果找不到 command 也直接跳过当前的 hook_def
            command = hook_def.get("command","")
            if not command:
                continue

            # 使用 Hook 上下文构建环境变量 (Hook Scripts 入参)
            env = dict(os.environ)
            if context:
                # 这里通过临时写入环境变量来传递 Hook 脚本的参数
                env["HOOK_EVENT"]       = event
                env["HOOK_TOOL_NAME"]   = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"]  = json.dumps(
                    context.get("tool_input",""), ensure_ascii=False)[:10000]
                # 这块对应的 Hook 应该是 PostToolUse , 只有PostToolUse才会有tool_output
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(
                        context["tool_output"])[:10000]

            try:
                r = subprocess.run(
                    command, shell=True, cwd=WORKDIR, env=env,
                    capture_output=True, text=True, timeout=HOOK_TIMEOUT,
                )

                # 0：继续执行；
                # 1：阻止工具调用
                # 2：注入messages然后执行工具
                match r.returncode:
                # 这里的 returncode 就是退出返回值
                    case 0:
                        #静默继续
                        if r.stdout.strip():
                            print(f" [hook: {event}] {r.stdout.strip()[:100]}")

                        # 可选结构化 stdout: 一个保持教学约定简单扩展点
                        try:
                            # 这里貌似还能修改 tool_input
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
                            pass # stdout 不是 JSON -- 对简单 Hook 很正常

                    case 1:
                        #阻止运行
                        result["blocked"] = True
                        reason = r.stderr.strip() or "被hook组织"
                        result["block_reason"] = reason
                        print(f" [hook:{event}] 已阻止： {reason[:200]}")

                    case 2:
                        #注入消息
                        msg = r.stderr.strip() #不知道为啥这里要用 stderr, 我想用 stdout, 感觉更合理一点
                        if msg:
                            result["messages"].append(msg)
                            print(f" [hook: {event}] 注入：{msg[:200]}")

            except subprocess.TimeoutExpired:
                print(f" [hook:{event}] 超时：（{HOOK_TIMEOUT}s）")
            except Exception as e:
                print(f" [hook:{event}] 错误 {e}")

        return result



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

SYSTEM = f"你是位于 {WORKDIR} 的编码代理。 使用工具解决任务"

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

def agent_loop(messages: list, hooks: HookManager) -> None:
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
        for block in response.content :
            if block.type != "tool_use":   #在一次回复中有多个block，例如 think block，text block，tool_call block，如果不是tool_call block，就跳过
                continue

            tool_input = dict(block.input or {})
            ctx = {"tool_name":block.name, "tool_input":tool_input}

            # -- PreToolUse hooks --
            pre_result = hooks.run_hooks("PreToolUse", ctx)

            # 将 Hook 消息注入到结果中
            for msg in pre_result.get("messages", []):
                result.append({
                    "type":"tool_result",
                    "tool_use_id" : block.id,
                    "content": f"[Hook 消息]： {msg}",
                })
            if pre_result.get("blocked", False):
                reason = pre_result.get("block_reason", "被 Hook 阻止")
                output = f"工具被 PreToolUse Hook 阻止： {reason}"
                result.append({
                    "type":"tool_result",
                    "tool_use_id":block.id,
                    "content":output,
                })
                continue

            # --如果没有被hook阻止，那么就执行工具--
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**tool_input) if handler else f"未知工具 ： {block.name}"
            except Exception as e:
                output = f"错误： {e}"
            print(f"> {block.name}: {str(output)[:200]}")

            # -- PostToolUse hooks --
            ctx["tool_output"] = output
            post_result = hooks.run_hooks("PostToolUse", ctx)

            # 注入后置 Hook 消息
            for msg in post_result.get("messages", []):
                output += f"\n [Hook 备注]： {msg}"


            result.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

        #将工具使用的结果通过 role: user ，加入到消息列表中
        messages.append({"role": "user", "content": result})


if __name__ == "__main__":
    # 初始化 HookManager
    hooks = HookManager()
    # 触发 SessionStart hooks
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input":{}})

    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history, hooks)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
