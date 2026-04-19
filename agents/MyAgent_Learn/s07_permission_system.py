# Harness： 安全 -- 意图与执行之间的衔接链路
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

import os
import re
import subprocess
from fnmatch import fnmatch
import time
from dataclasses import dataclass, field
from pathlib import Path

import json
from anthropic import Anthropic
from dotenv import load_dotenv

import yaml # 导入PyYAML库，用于解析markdown前置内容
from scripts.pywin32_testall import failures

from agents.s06_context_compact import PERSIST_THRESHOLD

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = (
    f"""你是一个位于 {WORKDIR} 的编码代理。使用工具来完成任务。
用户控制权限。某些工具调用可能会被拒绝。"""
)

# -- 权限模式 --
# 教学版从三个清晰模式开始。
MODES = ("default", "plan", "auto")

READ_ONLY_TOOLS = {"read_file", "bash_readonly"}

#修改状态的工具
WRITE_TOOLS = {"write_file", "edit_file", "bash"}

# -- Bash 安全验证 --
class BashSecurityValidator:
    """
    验证 Bash 命令 是否存在明显危险模式

    教学版本刻意保持简洁易读。
    先捕获少量高风险模式，再由权限流水线决定是拒绝还是询问用户。
    """
    VALIDATORS = [
        ("shell_metachar", r"[;&|`$]"),  # Shell 元字符
        ("sudo", r"\bsudo\b"),  # 权限提升
        ("rm_rf", r"\brm\s+(-[a-zA-Z]*)?r"),  # 递归删除
        ("cmd_substitution", r"\$\("),  # 命令替换
        ("ifs_injection", r"\bIFS\s*="),  # IFS 注入
    ]

    def validate(self, command: str) -> list:
        """
        对所有验证器检查 Bash 命令。

        返回 (验证器名称, 匹配模式) 元组列表，表示失败项。
        空列表表示命令通过了所有验证器。
        """
        failures = []
        for name, partten in self.VALIDATORS:
            if re.search(partten, command):
                failures.append((name, partten))
        return failures

    def is_safe(self, command: str) -> bool:
        # 检查命令是否包含危险模式, 如果包含任何危险模式, 则返回False
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


#权限流水线使用单例验证器
bash_validator = BashSecurityValidator()

# --权限规则--
# 规则顺序检查： 首个匹配生效。
# 格式：{"tool": "<工具名或*>", "path": "<glob或*>", "behavior": "allow|deny|ask"}
DEFAULT_RULES = [
    # 始终拒绝危险模式
    {"tool": "bash", "content":"rm -rf/", "behavior":"deny"},
    {"tool": "bash", "content":"sudo *", "behavior":"deny"},
    # 允许读取所有文件
    {"tool": "read_file", "path":"*", "behavior":"allow"},
]

class PermissionManager:
    """
    管理工具调用的权限角色。

    Pipeline： deny_rules -> mode_check -> allow_rules -> ask_user
    教学版本刻意保持决策路径简短，便于读者在添加
    更高级策略层之前自行实现。
    """
    def __init__(self, mode: str = "default", rules: list = None):
        if mode not in MODES:
            raise ValueError(f"未知模式: {mode}。请从 {MODES}中选择")
        self.mode = mode,
        self.rules = rules or DEFAULT_RULES # 配置默认规则，但允许自定义规则
        # 简单的拒绝计数有助于发现系统反复拒绝代理请求的情况。
        self.consecutive_denials = 0
        self.max_consecutive_denials = 3

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """
        返回 {"behavior": "allow"|"deny"|"ask", "reason": str}
        """
        # 步骤 0: Bash 安全验证（在拒绝规则之前）
        # 教学版本提前检查以提高清晰度。
        if tool_name == "bash":
            command = tool_input.get("command", "")
            failures = bash_validator.validate(command)
            if failures:
                # 严重模式（sudo、rm-rf）直接拒绝
                severe = {"sudo", "rm_rf"}
                severe_hits = [f for f in failures if f[0] in severe]
                if severe_hits:
                    desc = bash_validator.describe_failures(command)
                    return {"behavior": "deny",
                            "reason": f"Bash 验证器： {desc}"}
                #其他模式升级到询问（用户仍可批准）
                desc = bash_validator.describe_failures(command)
                return {"behavior": "ask",
                        "reason": f"Bash 验证器： {desc}"}

        # 步骤 1 ：拒绝规则（免疫绕过，始终优先检查）
        for rule in self.rules:
            if rule["behavior"] != "deny":
                continue
            if self._matches(rule, tool_name, tool_input):
                return {"behavior": "deny",
                        "reason": f"被拒绝规则拦截： {rule}"}

        # 步骤 2 ：基于模式的决策
        if self.mode == "plan":
            # 计划模式下：拒绝所有写操作，允许读取
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny",
                        "reason": f"计划模式下拒绝 {tool_name} 写入操作"}
            return {"behavior": "allow",
                    "reason": f"计划模式下允许 {tool_name} 读取操作"}
        if self.mode == "auto":
            # 自动模式： 自动允许只读工具，写入需询问
            if tool_name in READ_ONLY_TOOLS or tool_name == "read_file":
                return {"behavior": "allow",
                        "reason": "自动模式：只读工具自动批准"}
            #教学版：降级到允许规则，然年后询问
            pass

        # 步骤 3： 允许规则
        for rule in self.rules:
            if rule["behavior"] != "allow":
                continue
            if self._matches(rule, tool_name, tool_input):
                self.consecutive_denials = 0
                return {"behavior": "allow",
                        "reason": f"匹配允许规则： {rule}"}

        # 步骤 4: 询问用户（未匹配工具的默认行为）
        return {"behavior": "ask",
                "reason": f"无规则匹配 {tool_name}，正在询问用户"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """交互式审批提示， 返回True 表示批准"""
        preview = json.dumps(tool_input, ensure_ascii=False)[:200]
        print(f"\n [权限] {tool_name} : {preview}")
        try:
            answer = input("是否批准该操作？(y/n/always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False

        if answer == "always":
            # 添加此工具的永久允许规则
            self.rules.append({"tool":tool_name, "path":"*", "behavior":"allow"})
            self.consecutive_denials = 0
            return True
        if answer in ("y", "yes"):
            self.consecutive_denials = 0
            return True

        # 跟踪拒绝次数以触发熔断
        self.consecutive_denials += 1
        if self.consecutive_denials >= self.max_consecutive_denials:
            print(f"[{self.consecutive_denials}] 次连续拒绝--"
                  "建议切换到（/plan）计划模式")
        return False

    def _matches(self, rule: dict, tool_name: str, tool_input: dict) -> bool:
        """
        检查规则是否与工具调用匹配
        工具名称匹配：检查规则是否适用于指定的工具
        路径模式匹配：检查文件操作是否匹配路径模式
        内容模式匹配：检查Bash命令是否匹配命令模式
        """
        # 工具名称匹配
        if rule.get("tool") and rule["tool"] != "*":
            if rule["tool"] != tool_name:
                return False
        # 路径模式匹配
        if "path" in rule and rule["path"] != "*":
            path = tool_input.get("path", "")
            if not fnmatch(path, rule["path"]):
                return False
        #内容模式匹配（用于Bash命令）
        if "content" in rule:
            command = tool_input.get("command", "")
            if not fnmatch(command, rule["content"]):
                return False
        return True



#TODO:



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

    output = (result.stdout + result.stderr).strip() or "无输出"
    return output

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

def agent_loop(messages: list, perms: PermissionManager) -> None:
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

            # -- 权限检查 -- 通过或者允许后执行工具
            decision = perms.check(block.name, block.input)

            # 对应 deny
            if decision["behavior"] == "deny":
                output = f"权限被拒绝: {decision['reason']}"
                print(f"  [已拒绝] {block.name}: {decision['reason']}")
            # 对应 ask
            elif decision["behavior"] == "ask":
                if perms.ask_user(block.name, block.input or {}):
                    #如果询问允许通过，就可以执行工具
                    handler = TOOL_HANDLERS.get(block.name)
                    output = handler(**(block.input or {})) if handler else f"未知工具：{block.name}"
                    print(f"> {block.name} : {str(output)[:200]}")
                else:
                    output = f"{block.name} 工具被用户拒绝"
                    print(f" [用户决绝] {block.name} 工具调用")
            # 对应 allow
            else:
                handler = TOOL_HANDLERS.get(block.name)
                output = handler(**(block.input or {})) if handler else f"未知工具：{block.name}"
                print(f"> {block.name} : {str(output)[:200]}")

            print(f"> {block.name}\n: {str(output)[:200]}")
            result.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

        #将工具使用的结果通过 role: user ，加入到消息列表中
        messages.append({"role": "user", "content": result})


if __name__ == "__main__":
    # 启动时选择权限模式
    print("权限模式： default, plan, auto")
    mode_input = input("模式（default）").strip().lower() or "default"
    if mode_input not in MODES:
        mode_input = "default"
    perms = PermissionManager(mode_input)
    print(f"当前权限模式：{mode_input}")

    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "quit", ""):
            break

        # /mode 命令用于运行时切换模式
        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"已切换到 {[perms.mode]} 模式")
            else:
                print(f"用法 /mode <{'|'.join(MODES)}>")
            continue
        # /rules 命令用于现实当前规则
        if query.strip().lower() == "/rules":
            for i, rule in enumerate(perms.rules):
                print(f"{i}: {rule}")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history, perms)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
