# Harness: 持久化任务——超越单次对话周期的目标。
"""
s12_task_system_CN.py - 任务系统

任务以 JSON 文件形式持久化在 .tasks/ 目录下，因此它们能够在上下文压缩后依然存活。
每个任务都携带一个小型的依赖图：

- blockedBy: 必须先完成的前置任务
- blocks: 被此任务阻塞的后续任务

    .tasks/
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
      task_3.json  {"id":3, "blockedBy":[2], "blocks":[], ...}

    依赖解析：
    +----------+     +----------+     +----------+
    | task 1   | --> | task 2   | --> | task 3   |
    | 已完成   |     | 被阻塞   |     | 被阻塞   |
    +----------+     +----------+     +----------+
         |                ^
         +--- 完成任务 1 会从任务 2 的 blockedBy 中移除它

核心思想：任务状态存储在磁盘上，而非仅存在于对话内部，因此即使上下文被压缩也不会丢失。
这些是持久化的工作图任务，而非瞬时的运行时执行槽。

阅读顺序：
1. TaskManager: 磁盘上 TaskRecord 的结构。
2. TOOL_HANDLERS / TOOLS: 任务操作如何与常规工具一起进入同一个循环。
3. agent_loop: 持久化的工作状态如何反馈给模型。

最常见的困惑：
- 任务记录是一个持久化的工作项
- 它不是线程、后台槽位或工作进程

教学边界：
本章首先讲解持久化的工作图。
运行时执行槽和调度器将在后续章节引入。
"""
import datetime
import json
import os
import random
import platform
import re
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv
from anthropic import Anthropic, APIError

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
TASKS_DIR = WORKDIR / ".tasks"

SYSTEM = f"你是位于 {WORKDIR} 的编码智能体。使用任务工具来规划和跟踪工作。"

# -- TaskManager: 持久化任务图，增删改查 --
class TaskManager:
    """
    持久化任务储存。
    把它想象成"磁盘上的工作图"， 而不是 "当前运行的工作进程"
    """

    def __init__(self, task_dir: Path):
        self.dir = task_dir
        self.dir.mkdir(exist_ok=True)
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        """
        f.stem          - 获取文件的基本名称（不含扩展名），例如对于 "task_123.json"，stem 是 "task_123"
        .split("_")[1]  - 按下划线分割字符串，取第二部分，例如 "task_123" 分割后得到 ["task", "123"]，取索引1的元素 "123"
        int()           - 将字符串 "123" 转换为整数 123
        """
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        return max(ids) if ids else 0

    # 加载任务，返回json数据
    def _load(self, task_id:int) -> dict:
        path = self.dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"任务 {task_id} 不存在")
        # 读取文件内容并解析为JSON对象
        return json.loads(path.read_text(encoding="UTF-8"))

    def _save(self, task:dict):
        path = self.dir / f"task_{task["id"]}.json"
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="UTF-8")

    # 定义创建任务工具
    def create(self, subject: str, description: str) -> str:
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "blockedBy": [],    # 必须先完成的前置任务,(被那个任务阻塞)
            "blocks": [],       # 被此任务阻塞的后续任务（阻塞那个任务）
            "owner": "",
        }
        self._save(task)    #将任务保存到磁盘中
        self._next_id +=1   #每次创建后，任务ID增加1 ， 任务ID同时也是任务存到磁盘中的序号
        return json.dumps(task, indent=4)       #将任务的JSON字符串返回

    #获取某个任务详情, 返回任务的JSON字符串
    def get(self, task_id:int) -> str:
        return json.dumps(self._load(task_id), indent=4)

    # 定义更新任务工具
    def update(self, task_id:int, status: str = None, owner:str = None,
               add_blocked_by:list[int] = None, add_blocks:list[int] = None) -> str:
        task = self._load(task_id)
        if owner is not None:
            task["owner"] = owner
        if status:
            # 状态只能是 pending, in_progress, completed, deleted 其中一个
            if status not in ("pending ", "in_progress", "completed", "deleted"):
                raise ValueError(f"无效的状态 {status}")
            task["status"] = status
            # 当任务完成后，需要从其他任务的 blockBy 中移除它
            if status == "completed":
                self._clear_dependency(task_id)
        # 添加依赖任务，被那个任务阻塞
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
            # 双向：同时添加到其他任务的 blockedBy 中， 之前前面的任务id会阻塞后面的
            for blocked_id in add_blocks: #遍历所有当前任务阻塞的任务id，如果他们的 blockedBy 列表中不存在当前任务id，就添加到 blockedBy 列表中
                try:
                    blocked = self._load(blocked_id)
                    if task_id not in blocked["blockedBy"]:
                        blocked["blockedBy"].append(task_id)
                        self._save(blocked)
                except ValueError:
                    pass
        #最后将 task 保存到磁盘中， 同时使用 JSON 字符串返回 提供给大模型使用
        self._save(task)
        return json.dumps(task, indent=4)

    #遍历所有 task 所有task文件，如果它的 blockedBy 中包含 completed_id，就从 blockedBy 中移除 completed_id ,然后保存移除后的任务到磁盘
    def _clear_dependency(self, completed_id: int):
        """ 从其他任务的 blockedBy 列表中移除已完成任务. """
        for f in self.dir.glob("task_*.json"):
            task = json.loads(f.read_text(encoding="UTF-8"))
            if completed_id in task.get("blockedBy", []):
                task["blockedBy"].remove(completed_id)
                self._save(task)

    def list_all(self) -> str:
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text(encoding="UTF-8")))
        if not tasks:
            return "没有任务"
        lines = []
        for t in tasks:
            marker = {"pending":"[ ]",
                      "in_progress":"[>]",
                      "completed":"[X]",
                      "deleted":"[-]"}.get(t["status"], "[?]")
            blocked = f"(被阻塞：{t["blockedBy"]})" if t.get("blockedBy") else ""
            owner = f" 负责人={t["owner"]}" if t.get("owner") else ""
            lines.append(f"{marker} # {t["id"]}: {t["subject"]} {owner} {blocked}")

TASKS = TaskManager(TASKS_DIR)

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
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("owner"), kw.get("addBlockedBy"), kw.get("addBlocks")),
    "task_list":   lambda **kw: TASKS.list_all(),
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),
}

'''
Tool Schema
用于给模型描述工具的输入参数和输出结果
'''
TOOLS = [
    {"name": "bash", "description": "运行 Shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "将内容写入文件。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "在文件中替换指定文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "task_create", "description": "创建新任务。",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                   "description": {"type": "string"}},
                      "required": ["subject"]}},
    {"name": "task_update", "description": "更新任务状态、负责人或依赖关系。",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "integer"},
                                     "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                                     "owner": {"type": "string", "description": "团队成员认领任务时设置"},
                                     "addBlockedBy": {"type": "array", "items": {"type": "integer"}},
                                     "addBlocks": {"type": "array", "items": {"type": "integer"}}},
                      "required": ["task_id"]}},
    {"name": "task_list", "description": "列出所有任务及状态摘要。",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "根据 ID 获取任务的完整详情。",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "integer"}},
                      "required": ["task_id"]}},
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
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"未知工具: {block.name}"
                except Exception as e:
                    output = f"错误: {e}"
                print(f"> {block.name}: {str(output)[:200]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
