# Harness： 计划 -- 将当前会话计划保留在模型的处理范围之外。
'''
这个章节是关于一个轻量化的代办写入
模型能够重写当前计划，保证关注一个当前的激活步骤
并且当多轮对话后，如果模型没有更新计划，提醒模型更新计划
'''

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from openai import fine_tuning

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
PLAN_REMINDER_INTERVAL = 3

SYSTEM = f"""你是一个 Coding Agent 在{WORKDIR}工作。
你需要使用TODO工具来进行多步骤工作。
当一个任务包含多个步骤时，始终只保留一个步骤处于进行状态。
随着工作推进，及时更新计划。宁用工具，不尚空谈。
"""

# 计划项
@dataclass
class PlanItem:
    content: str
    status: str = "pending"
    active_form: str = ""

# 计划状态 包含多个计划项 和 距离上次更新轮数
@dataclass
class PlanningState:
    items: list[PlanItem] = field(default_factory=list)
    rounds_since_update: int = 0

class TodoManager:
    def __init__(self):
        self.state = PlanningState()

    def update(self, item: list) -> str:
        if len(item) > 12:
            raise ValueError("让对话计划更短（最多12个计划项）")

        normalized = []
        in_progress_count = 0
        for index, raw_item in enumerate(item):
            content = str(raw_item.get("content", "")).strip() # strip 去除字符串两端的空白字符（空格、制表符、换行符等），用于清理数据，避免前后多余的空白影响后续处理
            status = str(raw_item.get("status", "")).lower() # lower 转换为小写，确保状态值统一
            active_form = str(raw_item.get("active_form", "")).strip() # strip 去除字符串两端的空白字符（空格、制表符、换行符等），用于清理数据，避免前后多余的空白影响后续处理

            if not content:
                raise ValueError(f"计划项(Item) {index} 缺少内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"计划项(Item) {index} 状态值无效（{status}）")
            if status == "in_progress":
                in_progress_count += 1

            normalized.append(PlanItem(
                content,
                status,
                active_form))

            # 确保计划中只能有一个步骤处于进行状态
        if in_progress_count > 1:
            raise ValueError("计划中只能有一个步骤处于进行状态")

        self.state.items = normalized       # 更新计划项列表
        self.state.rounds_since_update = 0 # 重置距离上次更新轮数
        return self.render() # 将计划渲染为字符串输出
    def render(self) -> str:
        if not self.state.items:
            return "还没有生成对话计划"

        lines = ["\n"]
        lines.append("对话计划：")
        for item in self.state.items:
            maker = {
                "pending": "[__待办__] ",
                "in_progress": "[__进行中__] ",
                "completed": "[__已完成__] ",
            }[item.status]  # 使用item.status作为键，从字典中获取对应的中文标签
            line = f"{maker} {item.content}"
            if item.status == "in_progress" and item.active_form:
                line += f" ({item.active_form})"
            lines.append(line)

        """
        代码会检查 self.state.items 中的每一个任务
        对于每个任务，判断其 status 属性是否等于 "completed"
        如果是已完成状态，就计数1
        最后将所有计数相加，得到总的已完成任务数
        """
        completed = sum(1 for item in self.state.items if item.status == "completed")
        lines.append(f"\n({completed}/{len(self.state.items)} completed)\n") # (已完成数/总任务数 completed)
        return "\n".join(lines)

    # 记录当前轮次，增加距离上次更新轮数
    def note_round_without_update(self) -> None:
        self.state.rounds_since_update += 1

    # 输出提醒模型更新计划
    def reminder(self) -> str | None:
        if not self.state.items:
            return None
        if self.state.rounds_since_update < PLAN_REMINDER_INTERVAL:  #如果距离上次更新轮数小于提醒间隔，不提醒
            return None
        return "<reminder> 再继续执行前，请更新计划。 </reminder>"

TODO = TodoManager()

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
    "bash" : lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
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
        "name": "todo",
        "description": "Rewrite the current session plan for multi-step work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Optional present-continuous label.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["items"],
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
        used_todo = False
        for block in response.content :
            if block.type != "tool_use":   #在一次回复中有多个block，例如 think block，text block，tool_call block，如果不是tool_call block，就跳过
                continue

            handler = TOOL_HANDLERS.get(block.name) # 根据需要调用工具的名称然后获取 对应 工具的 Function Object
            try:
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
            except Exception as exc:
                output = f"Error: {exc}"

            # 拼接工具返回结果块，后续需用凭借进 role: user 中
            print(f"> {block.name} : {str(output)[:200]}")
            result.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(output)
            })

            if block.name == "todo":
                used_todo = True

        #如果使用了 计划（todo） 工具，重置一下计划更新计数器
        if used_todo:
            TODO.state.rounds_since_update = 0
        else:
            # 如果没有使用 计划 工具，重置计划更新计数器 + 1 ，如果超过 3 轮，就提醒一下更新计划
            TODO.note_round_without_update()
            reminder = TODO.reminder()   # 如果超过3轮，将  “<reminder> 再继续执行前，请更新计划。 </reminder>”  加入结果中
            if reminder:
                result.insert(0, {"type": "text", "text": reminder})

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
