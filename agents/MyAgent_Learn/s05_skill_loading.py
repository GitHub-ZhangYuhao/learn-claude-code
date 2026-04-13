# Harness： 按需获取知识 —— 低成本发掘技能，仅在需要时加载相关内容。
"""
s05_skill_loading.py - 技能加载

本章介绍双层技能模型：

1. 在系统提示中放入一个轻量级技能目录。
2. 仅当模型请求时才加载完整的技能内容。

这样可以保持提示词小巧，同时让模型能够访问可重用的、特定任务的指导。
"""

import os
import re
import subprocess
from dataclasses import dataclass
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
SKILLS_DIR = WORKDIR / "skills"

# 技能名称 ， 技能描述 ， 技能文件路径
@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path


# 技能文档类，包含技能清单和完整内容
@dataclass
class SkillDocument:
    """ 技能文档类，包含技能清单和完整内容 """
    manifest: SkillManifest
    body: str

# 技能注册表， 用于管理和加载技能
class SkillRegistry:
    """ 技能注册表， 用于管理和加载技能 """
    def __init__(self, skills_dir: Path):
        """初始化技能注册表"""
        self.skills_dir = skills_dir
        self.documents: dict[str, SkillDocument] = {} # 技能列表， key为技能名称，value为技能文档
        self._load_all() # 加载所有技能

    def _load_all(self) -> None:
        """
        加载所有技能
        """
        if not self.skills_dir.exists():    # exists()：Path 对象的方法，用于检查路径是否存在
            return

        # 遍历技能目录中的所有 SKILL.md 文件
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            meta, body = self._parse_frontmatter(path.read_text())
            name = meta.get("name", path.parent.name)
            description = meta.get("description", "No description")
            manifest = SkillManifest(name=name, description=description, path=path)
            self.documents[name] = SkillDocument(manifest=manifest, body=body.strip()) #  {"SkillName" : SkillDocument}  .strip()去掉首尾空格

    def _parse_frontmatter(self, text:str) -> tuple[dict, str]:     # ( 技能元数据 , 技能内容 )
        """
        解析markdown前置 metadata 和 技能内容
        """
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        """ r"..."：原始字符串，避免反斜杠转义
            ^---\n：匹配字符串开头的 --- 后跟换行符（前置内容的开始标记）
            (.*?)：第一个捕获组，非贪婪匹配任意字符（捕获 YAML 前置内容）
            \n---\n：匹配换行符后跟 --- 再后跟换行符（前置内容的结束标记）
            (.*)：第二个捕获组，贪婪匹配剩余的所有字符（捕获前置内容后的正文）
            re.DOTALL：特殊标志，使 . 可以匹配换行符（允许捕获组跨越多行） """
        if not match:
            return {}, text

        # 开始组装 metadata
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2)

    def describe_available(self) -> str:
        """ 描述可用技能名称和描述 用于塞入到 system prompt 中 """
        if not self.documents:
            return "(no skills available)"
        lines = []
        for name in sorted(self.documents):
            manifest = self.documents[name].manifest
            lines.append(f"- {manifest.name}: {manifest.description}")
        return "\n".join(lines)

    def load_full_text(self, name: str) -> str:
        document = self.documents.get(name)
        if not document:
            know = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: Unknow skill {name}. Available skills {know}"

        return (
            f"<skill name = \"{document.manifest.name}\">\n"
            f"{document.body}"
            f"</skill>"
        )

# 初始化技能注册表
SKILL_REGISTRY = SkillRegistry(SKILLS_DIR)


SYSTEM = f"""你是 {WORKDIR} 目录下的编码代理。
当某项任务在执行前需要专用指令时，请使用 load_skill。
可用技能：
{SKILL_REGISTRY.describe_available()}
"""

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

    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as exc:
        return f"Error: {exc}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {exc}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        content = file_path.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        file_path.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"

'''
Tool Handler
**kw：表示接收任意数量的关键字参数
提取可选的limit参数（使用kw.get("limit")，如果不存在则返回None）
'''
TOOL_HANDLERS = {
    "bash" :        lambda **kw: run_bash(kw["command"]),
    "read_file":    lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill":   lambda **kw: SKILL_REGISTRY.load_full_text(kw["name"]),     #本章节新增的工具
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
    {
        "name": "load_skill",
        "description": "Load the full body of a named skill into the current context.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
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

def agent_loop(messages: list) -> None:
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
            # 如果工具调用是 task 处理 SubAgent， 否则调用普通工具

            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            except Exception as exc:
                output = f"Error: {exc}"

            result.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

        #将工具使用的结果通过 role: user ，加入到消息列表中
        messages.append({"role": "user", "content": result})

if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "quit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
