#!/usr/bin/env python3
# Harness: safety -- the pipeline between intent and execution.
"""
s07_permission_system_CN.py - 权限系统（中文版）

每个工具调用在执行前都要经过权限流水线。

教学流水线：
  1. 拒绝规则
  2. 模式检查
  3. 允许规则
  4. 询问用户

此版本先介绍三种模式：
  - default（默认）
  - plan（计划）
  - auto（自动）

这足以构建一个可理解的权限系统，而不会在第一天就把所有高级策略分支塞给读者。

核心思想："安全是一个流水线，而不是布尔值。"
"""

import json
import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# -- 权限模式 --
# 教学版本从三种清晰模式开始。
MODES = ("default", "plan", "auto")

READ_ONLY_TOOLS = {"read_file", "bash_readonly"}

# 修改状态的工具
WRITE_TOOLS = {"write_file", "edit_file", "bash"}


# -- Bash 安全验证 --
class BashSecurityValidator:
    """
    验证 Bash 命令是否存在明显危险模式。

    教学版本刻意保持简洁易读。
    先捕获少量高风险模式，再由权限流水线决定是拒绝还是询问用户。
    """

    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),       # Shell 元字符
        ("sudo", r"\bsudo\b"),                 # 权限提升
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),  # 递归删除
        ("cmd_substitution", r"\$\("),          # 命令替换
        ("ifs_injection", r"\bIFS\s*="),        # IFS 注入
    ]

    def validate(self, command: str) -> list:
        """
        对所有验证器检查 Bash 命令。

        返回 (验证器名称, 匹配模式) 元组列表，表示失败项。
        空列表表示命令通过了所有验证器。
        """
        failures = []
        for name, pattern in self.VALIDATORS:
            if re.search(pattern, command):
                failures.append((name, pattern))
        return failures

    def is_safe(self, command: str) -> bool:
        """便捷方法：仅在所有验证器都通过时返回 True。"""
        return len(self.validate(command)) == 0

    def describe_failures(self, command: str) -> str:
        """返回验证失败的人类可读摘要。"""
        failures = self.validate(command)
        if not failures:
            return "未检测到问题"
        parts = [f"{name} (模式: {pattern})" for name, pattern in failures]
        return "安全标记: " + ", ".join(parts)


# -- 工作区信任 --
def is_workspace_trusted(workspace: Path = None) -> bool:
    """
    检查工作区是否被显式标记为可信。

    教学版本使用简单的标记文件。更完整的系统可以
    在此思路上叠加更丰富的信任流。
    """
    ws = workspace or WORKDIR
    trust_marker = ws / ".claude" / ".claude_trusted"
    return trust_marker.exists()


# 权限流水线使用的单例验证器
bash_validator = BashSecurityValidator()


# -- 权限规则 --
# 规则按顺序检查：首个匹配生效。
# 格式: {"tool": "<工具名或*>", "path": "<glob或*>", "behavior": "allow|deny|ask"}
DEFAULT_RULES = [
    # 始终拒绝危险模式
    {"tool": "bash", "content": "rm -rf /", "behavior": "deny"},
    {"tool": "bash", "content": "sudo *", "behavior": "deny"},
    # 允许读取所有文件
    {"tool": "read_file", "path": "*", "behavior": "allow"},
]


class PermissionManager:
    """
    管理工具调用的权限决策。

    流水线: deny_rules -> mode_check -> allow_rules -> ask_user

    教学版本刻意保持决策路径简短，便于读者在添加
    更高级策略层之前自行实现。
    """

    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"未知模式: {mode}。请从 {MODES} 中选择")
        self.mode = mode
        self.rules = rules or list(DEFAULT_RULES)
        # 简单的拒绝计数有助于发现系统反复拒绝
        # 代理请求的情况。
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        返回: {"behavior": "allow"|"deny"|"ask", "reason": str}
        """
        # 步骤 0: Bash 安全验证（在拒绝规则之前）
        # 教学版本提前检查以提高清晰度。
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                # 严重模式（sudo、rm_rf）直接拒绝
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny",
                            "reason": f"Bash 验证器: {desc}"}
                # 其他模式升级到询问（用户仍可批准）
                desc = bash_validator.describe_failures(command)
                return {"behavior": "ask",
                        "reason": f"Bash 验证器标记: {desc}"}

        # 步骤 1: 拒绝规则（免疫绕过，始终优先检查）
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny",
                        "reason": f"被拒绝规则拦截: {rule}"}

        # 步骤 2: 基于模式的决策
        if self.mode == "plan":
            # 计划模式: 拒绝所有写操作，允许读取
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny",
                        "reason": "计划模式: 写操作被禁止"}
            return {"behavior": "allow", "reason": "计划模式: 只读允许"}

        if self.mode == "auto":
            # 自动模式: 自动允许只读工具，写入需询问
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {"behavior": "allow",
                        "reason": "自动模式: 只读工具自动批准"}
            # 教学版: 降级到允许规则，然后询问
            pass

        # 步骤 3: 允许规则
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {"behavior": "allow",
                        "reason": f"匹配允许规则: {rule}"}

        # 步骤 4: 询问用户（未匹配工具的默认行为）
        return {"behavior": "ask",
                "reason": f"无规则匹配 {tool_name}，正在询问用户"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """交互式审批提示。返回 True 表示批准。"""
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n  [权限] {tool_name}: {preview}")
        try:
            answer = input("  允许? (y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer == "always":
            # 添加此工具的永久允许规则
            self.rules.append({"tool": tool_name, "path": "*", "behavior": "allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        # 跟踪拒绝次数以触发熔断
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"  [{self.consecutive_denials} 次连续拒绝 -- "
                  "建议切换到计划模式]")
        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        """检查规则是否与工具调用匹配。"""
        # 工具名称匹配
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
        # 路径模式匹配
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        # 内容模式匹配（用于 Bash 命令）
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False
        return True


# -- 工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"错误: 文本在 {path} 中未找到"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}

TOOLS = [
    {"name": "bash", "description": "运行 Shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中替换精确匹配的文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

SYSTEM = f"""你是一个位于 {WORKDIR} 的编码代理。使用工具来完成任务。
用户控制权限。某些工具调用可能会被拒绝。"""


def agent_loop(messages: list, perms: PermissionManager):
    """
    权限感知的代理循环。

    对每个工具调用：
      1. 大模型请求使用工具
      2. 权限流水线检查: deny_rules -> mode -> allow_rules -> ask
      3. 若允许：执行工具，返回结果
      4. 若拒绝：向大模型返回拒绝消息
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

            # -- 权限检查 --
            decision = perms.check(block.name, block.input or {})

            if decision["behavior"] == "deny":
                output = f"权限被拒绝: {decision['reason']}"
                print(f"  [已拒绝] {block.name}: {decision['reason']}")

            elif decision["behavior"] == "ask":
                if perms.ask_user(block.name, block.input or {}):
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**(block.input or {})) if handler else f"未知工具: {block.name}"
                    print(f"> {block.name}: {str(output)[:200]}")
                else:
                    output = f"{block.name} 被用户拒绝"
                    print(f"  [用户拒绝] {block.name}")

            else:  # allow
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**(block.input or {})) if handler else f"未知工具: {block.name}"
                print(f"> {block.name}: {str(output)[:200]}")

            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 启动时选择权限模式
    print("权限模式: default, plan, auto")
    mode_input = input("模式 (default): ").strip().lower() or "default"
    if mode_input not in MODES:
        mode_input = "default"

    perms = PermissionManager(mode=mode_input)
    print(f"[权限模式: {mode_input}]")

    history = []
    while True:
        try:
            query = input("\033[36ms07_CN >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # /mode 命令用于运行时切换模式
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"[已切换到 {parts[1]} 模式]")
            else:
                print(f"用法: /mode <{'|'.join(MODES)}>")
            continue

        # /rules 命令用于显示当前规则
        if query.strip() == "/rules":
            for i, rule in enumerate(perms.rules):
                print(f"  {i}: {rule}")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history, perms)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
