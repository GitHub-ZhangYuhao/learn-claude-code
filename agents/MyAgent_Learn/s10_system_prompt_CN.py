#!/usr/bin/env python3
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

import platform
import datetime
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

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="


class SystemPromptBuilder:
    """
    从独立段落拼装系统提示词。

    这里的教学目标很清晰：
    每个段落只有一个来源和一个职责。

    这使得提示词更易于推理、更易于测试，也更容易
    在智能体获得新能力时逐步演进。
    """

    def __init__(self, workdir: Path = None, tools: list = None):
        self.workdir = workdir or WORKDIR
        self.tools = tools or []
        self.skills_dir = self.workdir / "skills"
        self.memory_dir = self.workdir / ".memory"

    # -- 第1节：核心指令 --
    def _build_core(self) -> str:
        return (
            f"You are a coding agent operating in {self.workdir}.\n"
            "使用提供的工具来探索、读取、写入和编辑文件。\n"
            "在操作前务必验证，不要凭空猜测。优先读取文件而非猜测内容。"
        )

    # -- 第2节：工具列表 --
    def _build_tool_listing(self) -> str:
        if not self.tools:
            return ""
        lines = ["# 可用工具"]
        for tool in self.tools:
            props = tool.get("input_schema", {}).get("properties", {})
            params = ", ".join(props.keys())
            lines.append(f"- {tool['name']}({params}): {tool['description']}")
        return "\n".join(lines)

    # -- 第3节：技能元数据（来自 s05 概念的第1层） --
    def _build_skill_listing(self) -> str:
        if not self.skills_dir.exists():
            return ""
        skills = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            text = skill_md.read_text(encoding="utf-8")
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
            skills.append(f"- {name}: {desc}")
        if not skills:
            return ""
        return "# 可用技能\n" + "\n".join(skills)

    # -- 第4节：记忆内容 --
    def _build_memory_section(self) -> str:
        if not self.memory_dir.exists():
            return ""
        memories = []
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            text = md_file.read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
            if not match:
                continue
            header, body = match.group(1), match.group(2).strip()
            meta = {}
            for line in header.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            name = meta.get("name", md_file.stem)
            mem_type = meta.get("type", "project")
            desc = meta.get("description", "")
            memories.append(f"[{mem_type}] {name}: {desc}\n{body}")
        if not memories:
            return ""
        return "# 记忆（持久化）\n\n" + "\n\n".join(memories)

    # -- 第5节：CLAUDE.md 链 --
    def _build_claude_md(self) -> str:
        """
        按优先级顺序加载 CLAUDE.md 文件（全部包含）：
        1. ~/.claude/CLAUDE.md（用户全局指令）
        2. <项目根目录>/CLAUDE.md（项目指令）
        3. <当前子目录>/CLAUDE.md（目录特定指令）
        """
        sources = []

        # 用户全局
        user_claude = Path.home() / ".claude" / "CLAUDE.md"
        if user_claude.exists():
            sources.append(("用户全局 (~/.claude/CLAUDE.md)", user_claude.read_text(encoding="utf-8")))

        # 项目根目录
        project_claude = self.workdir / "CLAUDE.md"
        if project_claude.exists():
            sources.append(("项目根目录 (CLAUDE.md)", project_claude.read_text(encoding="utf-8")))

        # 子目录 — 在真实 CC 中，这会从 cwd 向上遍历到项目根目录
        # 教学：如果 cwd 不同于 workdir，则检查 cwd
        cwd = Path.cwd()
        if cwd != self.workdir:
            subdir_claude = cwd / "CLAUDE.md"
            if subdir_claude.exists():
                sources.append((f"子目录 ({cwd.name}/CLAUDE.md)", subdir_claude.read_text(encoding="utf-8")))

        if not sources:
            return ""
        parts = ["# CLAUDE.md 指令"]
        for label, content in sources:
            parts.append(f"## 来自 {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    # -- 第6节：动态上下文 --
    def _build_dynamic_context(self) -> str:
        lines = [
            f"当前日期: {datetime.date.today().isoformat()}",
            f"工作目录: {self.workdir}",
            f"模型: {MODEL}",
            f"平台: {platform.system()}",
        ]
        return "# 动态上下文\n" + "\n".join(lines)

    # -- 拼装所有段落 --
    def build(self) -> str:
        """
        从所有段落拼装完整的系统提示词。

        静态段落（1-5）与动态段落（6）之间通过
        DYNAMIC_BOUNDARY 标记分隔。在真实 CC 中，静态前缀
        会在轮次间缓存以节省提示词 token。
        """
        sections = []

        core = self._build_core()
        if core:
            sections.append(core)

        tools = self._build_tool_listing()
        if tools:
            sections.append(tools)

        skills = self._build_skill_listing()
        if skills:
            sections.append(skills)

        memory = self._build_memory_section()
        if memory:
            sections.append(memory)

        claude_md = self._build_claude_md()
        if claude_md:
            sections.append(claude_md)

        # 静态/动态边界
        sections.append(DYNAMIC_BOUNDARY)

        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)

        return "\n\n".join(sections)


def build_system_reminder(extra: str = None) -> dict:
    """
    构建一个 system-reminder 用户消息，用于每轮动态内容。

    教学版本将提醒放在稳定系统提示词之外，这样
    短生命周期的上下文不会与长生命周期的指令混在一起。
    """
    parts = []
    if extra:
        parts.append(extra)
    if not parts:
        return None
    content = "<system-reminder>\n" + "\n".join(parts) + "\n</system-reminder>"
    return {"role": "user", "content": content}


# -- 工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区范围: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 已拦截危险命令"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (120秒)"


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
            return f"错误: 在 {path} 中未找到指定文本"
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
    {"name": "bash", "description": "运行 shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中替换精确匹配的文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

# 全局提示词构建器
prompt_builder = SystemPromptBuilder(workdir=WORKDIR, tools=TOOLS)


def agent_loop(messages: list):
    """
    使用拼装好的系统提示词运行智能体循环。

    系统提示词在每次迭代时重新构建。在真实 CC 中，静态
    前缀会被缓存，只有动态后缀在每轮次变化。
    """
    while True:
        system = prompt_builder.build()
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**(block.input or {})) if handler else f"未知工具: {block.name}"
            except Exception as e:
                output = f"错误: {e}"
            print(f"> {block.name}: {str(output)[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output),
            })

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    # 启动时显示拼装好的提示词，用于教学目的
    full_prompt = prompt_builder.build()
    section_count = full_prompt.count("\n# ")
    print(f"[系统提示词已拼装: {len(full_prompt)} 字符, 约 {section_count} 个段落]")

    # /prompt 命令显示完整拼装后的提示词
    history = []
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        if query.strip() == "/prompt":
            print("--- 系统提示词 ---")
            print(prompt_builder.build())
            print("--- 结束 ---")
            continue

        if query.strip() == "/sections":
            prompt = prompt_builder.build()
            for line in prompt.splitlines():
                if line.startswith("# ") or line == DYNAMIC_BOUNDARY:
                    print(f"  {line}")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
