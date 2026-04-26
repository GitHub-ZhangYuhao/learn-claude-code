#!/usr/bin/env python3
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

import os
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
RUNTIME_DIR = WORKDIR / ".runtime-tasks"
RUNTIME_DIR.mkdir(exist_ok=True)
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"你是一个编码代理，工作目录为 {WORKDIR}。使用 background_run 执行长时间运行的命令。"

STALL_THRESHOLD_S = 45  # 任务被视为停滞的秒数


class NotificationQueue:
    """
    基于优先级的通知队列，支持同键折叠。

    折叠意味着新消息可以替换具有相同键的旧消息，
    从而避免上下文被过时更新淹没。
    """

    PRIORITIES = {"immediate": 0, "high": 1, "medium": 2, "low": 3}

    def __init__(self):
        self._queue = []  # (优先级, 键, 消息) 列表
        self._lock = threading.Lock()

    def push(self, message: str, priority: str = "medium", key: str = None):
        """添加消息到队列，如果键匹配则折叠（替换旧消息）。"""
        with self._lock:
            if key:
                # 折叠：移除具有相同键的已有消息
                self._queue = [(p, k, m) for p, k, m in self._queue if k != key]
            self._queue.append((self.PRIORITIES.get(priority, 2), key, message))
            self._queue.sort(key=lambda x: x[0])

    def drain(self) -> list[str]:
        """按优先级返回所有待处理消息，并清空队列。"""
        with self._lock:
            messages = [m for _, _, m in self._queue]
            self._queue.clear()
            return messages


# -- BackgroundManager: 线程执行 + 通知队列 --
class BackgroundManager:
    def __init__(self):
        self.dir = RUNTIME_DIR
        self.tasks = {}  # task_id -> {status, result, command, started_at}
        self._notification_queue = []  # 已完成任务的结果
        self._lock = threading.Lock()

    def _record_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _output_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"

    def _persist_task(self, task_id: str):
        """将任务记录持久化到 JSON 文件（UTF-8 编码）。"""
        record = dict(self.tasks[task_id])
        self._record_path(task_id).write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _preview(self, output: str, limit: int = 500) -> str:
        compact = " ".join((output or "(无输出)").split())
        return compact[:limit]

    def run(self, command: str) -> str:
        """启动后台线程，立即返回 task_id。"""
        task_id = str(uuid.uuid4())[:8]
        output_file = self._output_path(task_id)
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
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return (
            f"后台任务 {task_id} 已启动: {command[:80]} "
            f"(output_file={output_file.relative_to(WORKDIR)})"
        )

    def _execute(self, task_id: str, command: str):
        """线程目标：运行子进程，捕获输出，推入通知队列。"""
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "错误: 超时 (300秒)"
            status = "timeout"
        except Exception as e:
            output = f"错误: {e}"
            status = "error"
        final_output = output or "(无输出)"
        preview = self._preview(final_output)
        output_path = self._output_path(task_id)
        output_path.write_text(final_output, encoding="utf-8")
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = final_output
        self.tasks[task_id]["finished_at"] = time.time()
        self.tasks[task_id]["result_preview"] = preview
        self._persist_task(task_id)
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "preview": preview,
                "output_file": str(output_path.relative_to(WORKDIR)),
            })

    def check(self, task_id: str = None) -> str:
        """检查单个任务状态，或列出所有任务。"""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"错误: 未知任务 {task_id}"
            visible = {
                "id": t["id"],
                "status": t["status"],
                "command": t["command"],
                "result_preview": t.get("result_preview", ""),
                "output_file": t.get("output_file", ""),
            }
            return json.dumps(visible, indent=2, ensure_ascii=False)
        lines = []
        for tid, t in self.tasks.items():
            lines.append(
                f"{tid}: [{t['status']}] {t['command'][:60]} "
                f"-> {t.get('result_preview') or '(运行中)'}"
            )
        return "\n".join(lines) if lines else "无后台任务。"

    def drain_notifications(self) -> list:
        """返回并清空所有待完成的完成通知。"""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs

    def detect_stalled(self) -> list[str]:
        """
        返回运行时间超过 STALL_THRESHOLD_S 的任务 ID。
        """
        now = time.time()
        stalled = []
        for task_id, info in self.tasks.items():
            if info["status"] != "running":
                continue
            elapsed = now - info.get("started_at", now)
            if elapsed > STALL_THRESHOLD_S:
                stalled.append(task_id)
        return stalled


BG = BackgroundManager()


# -- 工具实现 --
def safe_path(p: str) -> Path:
    """安全地解析路径，确保不逃逸工作目录。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径逃逸出工作区: {p}")
    return path


def run_bash(command: str) -> str:
    """运行 shell 命令（阻塞式），带危险命令过滤。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误: 危险命令已被拦截"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误: 超时 (120秒)"


def run_read(path: str, limit: int = None) -> str:
    """读取文件内容（UTF-8 编码）。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (剩余 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件内容（UTF-8 编码）。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """替换文件中的精确文本（UTF-8 编码）。"""
    try:
        fp = safe_path(path)
        c = fp.read_text(encoding="utf-8")
        if old_text not in c:
            return f"错误: 在 {path} 中未找到文本"
        fp.write_text(c.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误: {e}"


TOOL_HANDLERS = {
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "background_run":   lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
}

TOOLS = [
    {"name": "bash", "description": "运行 shell 命令（阻塞）。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容（UTF-8）。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入文件内容（UTF-8）。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本（UTF-8）。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "background_run", "description": "在后台线程中运行命令，立即返回 task_id。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "check_background", "description": "检查后台任务状态。省略 task_id 则列出所有任务。",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
]


def agent_loop(messages: list):
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
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms13_CN >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
