# Harness: persistence -- 跨会话边界的记忆。
"""
s09_memory_system.py - 记忆系统

这个教学版聚焦一个核心想法：
有些信息应当在当前对话之后仍然保留，但并不是所有信息都该进入记忆。

应该记忆：
  - 用户偏好
  - 重复出现的用户反馈
  - 当前代码中不明显的项目事实
  - 外部资源的指针

不应该记忆：
  - 可以从仓库重新读取的代码结构
  - 临时的任务状态
  - 机密信息

存储布局：
  .memory/
    MEMORY.md
    prefer_tabs.md
    review_style.md
    incident_board.md

每条记忆都是一个带前置信息的 Markdown 文件。
代理可以通过 save_memory() 保存记忆，
每次写入后都会重建记忆索引。

可选的“Dream”过程可以在之后合并、去重、清理已存储的记忆。
它很有用，但不是读者首先需要理解的内容。

关键洞见：
“记忆只存储跨会话仍值得回忆、且无法从当前仓库轻易重新推导的信息。”
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

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference")
MAX_INDEX_LINES = 200

class MemoryManager:
    """
    跨会话加载， 构建并保存持久记忆

    这个教学版让记忆保持显式：
    每条记忆一个 Markdown 文件， 加一个紧凑的索引文件
    """

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memories = {} # list: name -> {description, type, content}

    def load_all(self):
        """
        加载MEMORY.md 索引和所有单独的记忆文件
        """
        self.memories = {}
        if not self.memory_dir.exists():
            return

        #扫描除 MEMORY.md 之外的所有的 .md 文件
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file":md_file.name
                }

        count = len(self.memories)
        if count>0:
            print(f"[已加载记忆： 来自{self.memory_dir} 的 {count} 条记忆]")

    def load_memory_prompt(self) -> str:
        """构建用于注入系统提示的记忆片段。"""
        if not self.memories:
            return ""

        sections = []
        sections.append("# Memories (persistent across sessions)")
        sections.append("")

        # 按类型分组，便于阅读
        for mem_type in MEMORY_TYPES:
            typed = {k: v for k, v in self.memories.items() if v["type"] == mem_type}
            if not typed:
                continue
            sections.append(f"## [{mem_type}]")
            for name, mem in typed.items():
                sections.append(f"### {name}: {mem['description']}")
                if mem["content"].strip():
                    sections.append(mem["content"].strip())
                sections.append("")

        return "\n".join(sections)

    def save_memory(self, name: str, description: str, mem_type: str, content: str) -> str:
        """
        将记忆保存到磁盘并更新索引。

        返回状态消息
        """
        if mem_type not in MEMORY_TYPES:
            return f"错误： type 必须是 {MEMORY_TYPES} 之一"

        # 清理名称以用于文件名
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())    # 只允许字母、数字、下划线和短横线
        if not safe_name:
            return "错误：无效的记忆名称"

        self.memory_dir.mkdir(parents=True, exist_ok=True) #确保记忆目录存在，不存在就创建，已存在也不报错

        # 写入带强制信息的单独记忆文件
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {type}\n"
            f"---\n"
            f"{content}\n"
        )
        #将记忆写入磁盘
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(frontmatter, encoding="utf-8")

        # 更新内存中的记忆存储
        self.memories[name] = {
            "description":description,
            "type":mem_type,
            "content":content,
            "file":file_name
        }

        #重建 MEMORY.md 索引
        self._rebuild_index()
        return f"已保存记忆 '{name}' [{mem_type}] 到 {file_path.relative_to(WORKDIR)}"

    def _rebuild_index(self):
        """根据当前内存状态重建 MEMORY.md, 最多200行."""
        lines = [" # Memory Index", ""]
        for name, mem in self.memories.items():
            lines.append(f"- {name}: {mem["description"]} [{mem['type']}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"...(truncated at {MAX_INDEX_LINES} lines)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _parse_frontmatter(self, text: str) -> dict:
        """解析以 --- 风格的钳制信息与正文内容。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            return None
        header, body = match.group(1), match.group(2)
        result = {"content": body.strip()}
        for line in header.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
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

# 全局记忆管理器
memory_mgr = MemoryManager()

def run_save_memory(name: str, description: str, mem_type: str, content: str) -> str:
    return memory_mgr.save_memory(name, description, mem_type, content)

"""
工具调用处理函数
"""
TOOL_HANDLERS = {
    "bash":         lambda **kw : run_bash(kw["command"]),
    "read_file":    lambda **kw : run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw : run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw : run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "save_memory":  lambda **kw : run_save_memory(kw["name"], kw["description"], kw["type"], kw["content"])
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
    {"name": "save_memory", "description": "Save a persistent memory that survives across sessions.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Short identifier (e.g. prefer_tabs, db_schema)"},
         "description": {"type": "string", "description": "One-line summary of what this memory captures"},
         "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                  "description": "user=preferences, feedback=corrections, project=non-obvious project conventions or decision reasons, reference=external resource pointers"},
         "content": {"type": "string", "description": "Full memory content (multi-line OK)"},
     }, "required": ["name", "description", "type", "content"]}},
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
def build_system_prompt() -> str:
    """组装系统提示， 并包含记忆内容"""
    parts = [f"你是位于 {WORKDIR} 的编码代理。请使用工具完成任务。"]

    # 如有可用记忆则注入记忆内容
    memory_section = memory_mgr.load_memory_prompt()
    if memory_section:
        parts.append(memory_section)

    parts.append(MEMORY_GUIDANCE)
    return "\n\n".join(parts)

def agent_loop(messages: list) -> None:
    """
    带记忆感知系统提示的代理循环。

    每次调用都会重建系统提示， 使新保存的记忆再同一绘画的下一次 LLM 轮次中可见。
    """
    while True:
        system = build_system_prompt()
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
    # 会话开始时加载已有记忆
    memory_mgr.load_all()
    mem_count = len(memory_mgr.memories)
    if mem_count:
        print(f"[已将 {mem_count} 条记忆加载到上下文中]")
    else:
        print("[暂无记忆。可通过 save_memory 创建记忆]")

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
