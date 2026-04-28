# Harness: 后台执行 —— 模型在思考的同时，harness 等待后台结果。
"""
s13_background_tasks_CN.py - 后台任务

在后台线程中运行耗时命令。每次 LLM 调用前，循环会从通知队列中
排出已完成的结果并交还给模型。

    主线程                     后台线程
    +-----------------+        +-----------------+
    | agent loop      |        | 任务执行          |
    | ...             |        | ...             |
    | [LLM 调用] <---+------- | 入队(result)     |
    |  ^排空队列       |        +-----------------+
    +-----------------+

    时间线:
    Agent ----[启动 A]----[启动 B]----[其他工作]----
                  |              |
                  v              v
               [A 运行]      [B 运行]
                  |              |
                  +-- 通知队列 --> [结果注入]

此处的后台任务是运行时执行槽位，而非 s12 中引入的持久化任务面板记录。
"""

import datetime
import json
import os
import subprocess
import threading
import time
import uuid

from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic, APIError

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
RUNTIME_DIR = WORKDIR / ".runtime-tasks"
RUNTIME_DIR.mkdir(exist_ok=True)
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个编码代理，工作目录为 {WORKDIR}。使用 background_run 执行长时间运行的命令。"

STALL_THRESHOLD_S = 45  # 任务被视为停滞的秒数

# 安全路径解析函数,确保只在安全工作区内操作
def safe_path(path_str: str) -> Path:
    path = (WORKDIR / path_str).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path_str}")
    return path

class BackgroundManager:
    def __init__(self):
        self.dir = RUNTIME_DIR
        self.tasks = {}
        self._notification_queue = []   # 已完成任务的结果
        self._lock = threading.Lock()

    def _record_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _output_path(self, task_id:str) -> Path:
        return self.dir / f"{task_id}.log"

    def _persist_task(self, task_id: str):
        """ 将任务记录持久化 到 JSON 文件（UTF-8编码）写入磁盘 """
        record = dict(self.tasks[task_id])
        # 获取输出文件路径， 然后将这个文件写入 json数据
        self._record_path(task_id).write_text(
            json.dumps(record, indent=4, ensure_ascii=False),
            encoding="UTF-8",
        )

    def _preview(self, output: str, limit: int = 500) -> str:
        compact = " ".join((output or "无输出").split())
        return compact[:limit]

    # 大语言模型 的 AI 工具接口
    def run(self, command: str) -> str:
        """启动后台线程，立即返回 task_id"""
        task_id = str(uuid.uuid4())[:8]     #创建一个唯一 id
        output_file = self._output_path(task_id)    #确定输出文件的路径
        self.tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "result": None,
            "command": command,
            "started_at": time.time(),
            "finished_at": None,
            "result_preview": "",
            "output_file": str(output_file.relative_to(WORKDIR)),
        }
        self._persist_task(task_id)
        # 创建 子线程任务 并 开始
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return (
            f"后台任务 {task_id} 已启动：{command[:80]}"
            f"(output_file = {output_file.relative_to(WORKDIR)})"
        )

    # 执行后台任务的函数
    def _execute(self, task_id:str, command: str):
        try:
            #创建一个子进程来执行 command ， 获取输出，然后推入通知队列
            r = subprocess.run(
                command, shell = True, cwd = WORKDIR,
                capture_output=True, text = True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "错误：超时（300秒）"
            status = "timeout"
        except Exception as e:
            output = f"错误{e}"
            status = "error"
        final_output = output or "(无输出)"
        preview = self._preview(final_output)
        # 截断输出结果作为 preview ，写入任务输出到 log
        output_path = self._output_path(task_id)
        output_path.write_text(final_output, encoding="UTF-8")
        # 更新 json 任务状态，更新状态和输出
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = final_output
        self.tasks[task_id]["finished_at"] = time.time()
        self.tasks[task_id]["result_preview"] = preview
        self._persist_task(task_id)         #更新任务 json 文件，更新状态和输出
        # 获取 线程锁 向线程共享列表写入 完成的任务, 和任务相关的数据和状态
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command":command[:80],
                "preview": preview,
                "output_file": str(output_path.relative_to(WORKDIR)),
            })

    #  大语言模型的 工具的接口
    def check(self, task_id: str = None)-> str:
        """ 检查单个任务状态，或列出所有任务 """
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误: 未知任务 {task_id}"
            visible = {
                "id" : t["id"],
                "status" : t["status"],
                "command" : t["command"],
                "result_preview" : t.get("result_preview", ""),
                "output_file": t.get("output_file", ""),
            }
            return json.dumps(visible, indent=4, ensure_ascii=False)

        # 输出每个任务的 status， command 和 result_preview
        lines = []
        for tid, t in self.tasks.items():
            lines.append(
                f"{tid}: [{t['status']}] {t['command'][:60]}"
                f" -> {t.get('result_preview') or '(运行中)'}"
            )
        return "\n".join(lines) if lines else "无后台任务。"

    def drain_notifications(self) -> list:
        """返回并清空所有待完成的完成通知。"""
        #请求线程锁，获取子线程写入的结果并且清空任务列表
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs

BG = BackgroundManager()


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
    "bash":             lambda **kw : run_bash(kw["command"]),
    "read_file":        lambda **kw : run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw : run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw : run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # 后台命令工具
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
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
    # 后台命令工具
    {"name": "background_run", "description": "在后台线程中运行命令，立即返回 task_id。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "检查后台任务状态。省略 task_id 则列出所有任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
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
        # 在下次模型调用前，排出后台通知并注入为合成对话对（教学演示行为）。
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
            f"[bg:{n['task_id']}] {n['status']}: {n['preview']} "
            f"(output_file={n['output_file']})"
            for n in notifs
            )
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})

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
