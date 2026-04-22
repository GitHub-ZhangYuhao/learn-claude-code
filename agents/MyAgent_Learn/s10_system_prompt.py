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

import json
import os
import re
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

DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="

class SystemPromptBuilder:
    """
    将系统提示词由独立的模块组合而成。
    此处的设计宗旨在于清晰明确：
    每个模块对应一个来源，承担一项职责。
    这使得提示词更易于梳理逻辑、更便于测试，并且在智能体拓展新功能时，也更易于迭代优化。
    """

    def __init__(self, workdir: Path = None, tools : list = None):
        self.workdir = workdir or WORKDIR
        self.tools = tools or []
        self.skills_dir = self.workdir / "skills"
        self.memory_dir = self.workdir / ".memory"

    # -- 第1节:核心指令 --
    def _build_core(self) -> str:
        return (
            f"You are a coding agent operating in {self.workdir}.\n"
            "使用提供的工具来探索、读取、写入和编辑文件。\n"
            "在操作前务必验证，不要凭空猜测。优先读取文件而非猜测内容。"
        )

    # -- 第2节:工具列表 --
    def _build_tool_listing(self) -> str:
        if not self.tools:
            return ""
        lines = ["# 可用工具"]
        for tool in self.tools:
            """
            工具列表项
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            """
            props = tool.get("input_schema", {}).get("properties", {})  # "properties": {"command": {"type": "string"}},
            params = ", ".join(props.keys())
            lines.append(f"- {tool['name']}:({params}): {tool['description']}")
        return  "\n".join(lines)

    # -- 第3节:技能元数据 --
    def _build_skill_listing(self) -> str:
        if not self.skills_dir.exists():
            return ""
        skills = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            text = skill_md.read_text(encoding="UTF-8")
            # 解析 frontmatter 获取名称和描述
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if not match:
                continue
            meta = {}
            for line in match.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            name = meta.get("name", skill_dir.name)
            desc = meta.get("description", "")
            skills.append(f" - {name}: {desc}")
        if not skills:
            return ""
        return "# 可用技能\n" + "\n".join(skills)

    def _build_memory_section(self) -> str:
        if not self.memory_dir.exits():
            return ""
        memories = []
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            text = md_file.read_text(encoding="utf-8")
            #TODO: continue

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

# Global prompt builder
prompt_builder = SystemPromptBuilder(workdir=WORKDIR, tools=TOOLS)


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


MEMORY_GUIDANCE = """
何时保存记忆：
- 用户表达偏好（“我喜欢用 Tab”“总是用 pytest”）-> type: user
- 用户纠正你（“不要做 X”“之所以错误是因为……”）-> type: feedback
- 你获知一条不易从当前代码直接推断的项目事实
  （例如：某规则出于合规要求，或某遗留模块必须保持不动）-> type: project
- 你获知外部资源所在位置（工单看板、仪表盘、文档 URL）
  -> type: reference

何时不保存：
- 能从代码直接推导的内容（函数签名、文件结构、目录布局）
- 临时任务状态（当前分支、打开的 PR 号、当前 TODO）
- 机密或凭据（API key、密码）
"""

def agent_loop(messages: list) -> None:
    """
    带记忆感知系统提示的代理循环。

    每次调用都会重建系统提示， 使新保存的记忆再同一绘画的下一次 LLM 轮次中可见。
    """
    while True:

        response = client.messages.create(
            model=MODEL,
            system=system,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role":"assistant", "content":response.content })

        # 如果大模型不再调用工具了，就结束循环
        if response.stop_reason != "tool_use":
            return

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
    # 启动时显示拼装号的提示词，用于教学目的
    full_prompt = prompt_builder.build()

    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # /memories 命令：列出当前记忆
        if query.strip() == "/memories":
            if memory_mgr.memories:
                for name, mem in memory_mgr.memories.items():
                    print(f" [{mem['type']}] {name} : {mem['description']}")
            else:
                print(" (无记忆)")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
