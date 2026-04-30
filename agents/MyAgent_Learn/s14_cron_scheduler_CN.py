# -*- coding: utf-8 -*-
#!/usr/bin/env python3
# Harness: time -- the agent schedules its own future work.
"""
s14_cron_scheduler.py - 定时任务调度

Agent 可以使用标准 cron 表达式将提示词调度到未来执行。当调度时间到达时，
它会将通知推回主对话循环中。

    Cron 表达式：5 个字段
    +---------+---------+-----------+---------+-----------+
    | 分钟     | 小时     | 日        | 月       | 星期       |
    | 0-59    | 0-23    | 1-31     | 1-12    | 0-6       |
    +---------+---------+-----------+---------+-----------+
    示例：
      "*/5 * * * *"   -> 每 5 分钟
      "0 9 * * 1"     -> 周一上午 9:00
      "30 14 * * *"   -> 每天下午 2:30

    两种持久化模式：
    +--------------------+-------------------------------+
    | session-only       | 内存列表，退出后丢失              |
    | durable            | .claude/scheduled_tasks.json  |
    +--------------------+-------------------------------+

    两种触发模式：
    +--------------------+-------------------------------+
    | recurring          | 重复执行，直到删除或 7 天后自动过期  |
    | one-shot           | 触发一次后自动删除                 |
    +--------------------+-------------------------------+

    抖动：重复任务可以避开精确的分钟边界。

    架构：
    +-------------------------------+
    |  后台线程                      |
    |  （每秒检查一次）                |
    |                               |
    |  遍历每个任务：                 |
    |    if cron_matches(now):      |
    |      入队通知                  |
    +-------------------------------+
              |
              v
    [notification_queue]
              |
         （在 agent_loop 顶部排出）
              |
              v
    [在 LLM 调用前注入为用户消息]

核心理念：记住未来的工作，然后在时间到达时交还给同一个主循环。
"""

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue, Empty

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SCHEDULED_TASKS_FILE = WORKDIR / ".claude" / "scheduled_tasks.json"
CRON_LOCK_FILE = WORKDIR / ".claude" / "cron.lock"
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = [0, 30]  # 对重复任务避开这些精确分钟
JITTER_OFFSET_MAX = 4     # 偏移范围（分钟）
# 教学版本：需要时使用 1-4 分钟的偏移。


class CronLock:
    """
    基于 PID 文件的锁，防止多个会话触发同一个定时任务。
    """

    def __init__(self, lock_path: Path = None):
        self._lock_path = lock_path or CRON_LOCK_FILE

    def acquire(self) -> bool:
        """
        尝试获取定时任务锁。成功返回 True。

        如果锁文件存在，检查里面的 PID 是否仍然存活。
        如果进程已死，说明锁已过期，我们可以接管。
        """
        if self._lock_path.exists():
            try:
                stored_pid = int(self._lock_path.read_text().strip())
                # PID 存活检测：发送信号 0（空操作）检查进程是否存在
                os.kill(stored_pid, 0)
                # 进程存活 -- 锁被另一个会话持有
                return False
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                # 过期锁（进程已死或 PID 无法解析）-- 移除它
                pass
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.write_text(str(os.getpid()))
        return True

    def release(self):
        """如果锁文件属于当前进程，则移除它。"""
        try:
            if self._lock_path.exists():
                stored_pid = int(self._lock_path.read_text().strip())
                if stored_pid == os.getpid():
                    self._lock_path.unlink()
        except (ValueError, OSError):
            pass


def cron_matches(expr: str, dt: datetime) -> bool:
    """
    检查 5 字段 cron 表达式是否匹配给定时间。

    字段：分钟 小时 日 月 星期
    支持：*（任意）、*/N（每隔 N）、N（精确）、N-M（范围）、N,M（列表）

    不依赖外部库 -- 纯手动匹配。
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
    # Python 的 weekday：0=周一；cron：0=周日。需要转换。
    cron_dow = (dt.weekday() + 1) % 7
    values[4] = cron_dow
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]

    for field, value, (lo, hi) in zip(fields, values, ranges):
        if not _field_matches(field, value, lo, hi):
            return False
    return True


def _field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """匹配单个 cron 字段与一个值。"""
    if field == "*":
        return True

    for part in field.split(","):
        # 处理步进：*/N 或 N-M/S
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)

        if part == "*":
            # */N -- 检查值是否在步进网格上
            if (value - lo) % step == 0:
                return True
        elif "-" in part:
            # 范围：N-M
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            # 精确值
            if int(part) == value:
                return True

    return False


class CronScheduler:
    """
    管理定时任务，含后台检查线程。

    教学版本只保留核心组件：任务记录、时间检查器、可选持久化和通知队列。
    """

    def __init__(self):
        self.tasks = []        # 任务字典列表
        self.queue = Queue()   # 通知队列
        self._stop_event = threading.Event()
        self._thread = None
        self._last_check_minute = -1  # 避免同一分钟内重复触发

    def start(self):
        """加载持久化任务并启动后台检查线程。"""
        self._load_durable()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        count = len(self.tasks)
        if count:
            print(f"[定时任务] 加载了 {count} 个任务")

    def stop(self):
        """停止后台线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def create(self, cron_expr: str, prompt: str,
               recurring: bool = True, durable: bool = False) -> str:
        """创建新的定时任务。返回任务 ID。"""
        task_id = str(uuid.uuid4())[:8]
        now = time.time()

        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt": now,
        }

        # 抖动：对于重复任务，如果 cron 在 :00 或 :30 触发，
        # 记录偏移以便稍作延迟
        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron_expr)

        self.tasks.append(task)
        if durable:
            self._save_durable()

        mode = "重复" if recurring else "单次"
        store = "持久化" if durable else "仅会话"
        return f"已创建任务 {task_id}（{mode}，{store}）：cron={cron_expr}"

    def delete(self, task_id: str) -> str:
        """按 ID 删除一个定时任务。"""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        if len(self.tasks) < before:
            self._save_durable()
            return f"已删除任务 {task_id}"
        return f"未找到任务 {task_id}"

    def list_tasks(self) -> str:
        """列出所有定时任务。"""
        if not self.tasks:
            return "没有定时任务。"
        lines = []
        for t in self.tasks:
            mode = "重复" if t["recurring"] else "单次"
            store = "持久化" if t["durable"] else "会话"
            age_hours = (time.time() - t["createdAt"]) / 3600
            lines.append(
                f"  {t['id']}  {t['cron']}  [{mode}/{store}] "
                f"（已创建 {age_hours:.1f} 小时）：{t['prompt'][:60]}"
            )
        return "\n".join(lines)

    def drain_notifications(self) -> list[str]:
        """排出队列中所有待处理的通知。"""
        notifications = []
        while True:
            try:
                notifications.append(self.queue.get_nowait())
            except Empty:
                break
        return notifications

    def _compute_jitter(self, cron_expr: str) -> int:
        """如果 cron 在 :00 或 :30 触发，返回一个小偏移（1-4 分钟）。"""
        fields = cron_expr.strip().split()
        if len(fields) < 1:
            return 0
        minute_field = fields[0]
        try:
            minute_val = int(minute_field)
            if minute_val in JITTER_MINUTES:
                # 基于表达式哈希的确定性抖动
                return (hash(cron_expr) % JITTER_OFFSET_MAX) + 1
        except ValueError:
            pass
        return 0

    def _check_loop(self):
        """后台线程：每秒检查是否有任务到期。"""
        while not self._stop_event.is_set():
            now = datetime.now()
            current_minute = now.hour * 60 + now.minute

            # 每分钟只检查一次，避免重复触发
            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)

            self._stop_event.wait(timeout=1)

    def _check_tasks(self, now: datetime):
        """检查所有任务，触发到期的任务。"""
        expired = []
        fired_oneshots = []

        for task in self.tasks:
            # 自动过期：超过 7 天的重复任务
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue

            # 对匹配检查应用抖动偏移
            check_time = now
            jitter = task.get("jitter_offset", 0)
            if jitter:
                check_time = now - timedelta(minutes=jitter)

            if cron_matches(task["cron"], check_time):
                notification = (
                    f"[定时任务 {task['id']}]: {task['prompt']}"
                )
                self.queue.put(notification)
                task["last_fired"] = time.time()
                print(f"[定时任务] 触发：{task['id']}")

                if not task["recurring"]:
                    fired_oneshots.append(task["id"])

        # 清理已过期和已执行的单次任务
        if expired or fired_oneshots:
            remove_ids = set(expired) | set(fired_oneshots)
            self.tasks = [t for t in self.tasks if t["id"] not in remove_ids]
            for tid in expired:
                print(f"[定时任务] 自动过期：{tid}（超过 {AUTO_EXPIRY_DAYS} 天）")
            for tid in fired_oneshots:
                print(f"[定时任务] 单次任务已完成并移除：{tid}")
            self._save_durable()

    def _load_durable(self):
        """从 .claude/scheduled_tasks.json 加载持久化任务。"""
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text(encoding="utf-8"))
            # 只加载持久化任务
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            print(f"[定时任务] 加载任务出错：{e}")

    def detect_missed_tasks(self) -> list[dict]:
        """
        启动时，检查每个持久化任务的 last_fired 时间。

        如果一个任务在会话关闭期间应该触发过（即 last_fired 和现在之间
        至少包含一次 cron 匹配），则标记为遗漏。调用者可以决定是执行还是
        丢弃每个遗漏的任务。

        """
        now = datetime.now()
        missed = []
        for task in self.tasks:
            last_fired = task.get("last_fired")
            if last_fired is None:
                continue
            last_dt = datetime.fromtimestamp(last_fired)
            # 从 last_fired 开始逐分钟向前走到 now（最多 24 小时）
            check = last_dt + timedelta(minutes=1)
            cap = min(now, last_dt + timedelta(hours=24))
            while check <= cap:
                if cron_matches(task["cron"], check):
                    missed.append({
                        "id": task["id"],
                        "cron": task["cron"],
                        "prompt": task["prompt"],
                        "missed_at": check.isoformat(),
                    })
                    break  # 一次遗漏足以标记它
                check += timedelta(minutes=1)
        return missed

    def _save_durable(self):
        """将持久化任务保存到磁盘。"""
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(
            json.dumps(durable, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )


# 全局调度器
scheduler = CronScheduler()


# -- 工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区范围：{p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：危险命令已被拦截"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "（无输出）"
    except subprocess.TimeoutExpired:
        return "错误：超时（120 秒）"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... （剩余 {len(lines) - limit} 行）"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"已写入 {len(content)} 字节"
    except Exception as e:
        return f"错误：{e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding="utf-8")
        if old_text not in content:
            return f"错误：文件中未找到文本：{path}"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


TOOL_HANDLERS = {
    "bash":        lambda **kw: run_bash(kw["command"]),
    "read_file":   lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":  lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":   lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "cron_create": lambda **kw: scheduler.create(
        kw["cron"], kw["prompt"], kw.get("recurring", True), kw.get("durable", False)),
    "cron_delete": lambda **kw: scheduler.delete(kw["id"]),
    "cron_list":   lambda **kw: scheduler.list_tasks(),
}

TOOLS = [
    {"name": "bash", "description": "运行 Shell 命令。",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "读取文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "写入文件内容。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "替换文件中的精确文本。",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "cron_create", "description": "使用 cron 表达式调度重复或单次任务。",
     "input_schema": {"type": "object", "properties": {
         "cron": {"type": "string", "description": "5 字段 cron 表达式：'分钟 小时 日 月 星期'"},
         "prompt": {"type": "string", "description": "任务触发时要注入的提示词"},
         "recurring": {"type": "boolean", "description": "true=重复，false=单次触发后删除。默认 true。"},
         "durable": {"type": "boolean", "description": "true=持久化到磁盘，false=仅会话。默认 false。"},
     }, "required": ["cron", "prompt"]}},
    {"name": "cron_delete", "description": "按 ID 删除一个定时任务。",
     "input_schema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "要删除的任务 ID"},
     }, "required": ["id"]}},
    {"name": "cron_list", "description": "列出所有定时任务。",
     "input_schema": {"type": "object", "properties": {}}},
]

SYSTEM = f"你是一个编码助手，工作目录为 {WORKDIR}。使用工具完成任务。\n\n你可以使用 cron_create 调度未来的工作。任务会自动触发，其提示词将注入到对话中。"


def agent_loop(messages: list):
    """
    定时感知 Agent 循环。

    在每次 LLM 调用前，排出通知队列并将已触发的任务提示词
    注入为用户消息。这就是 Agent "醒来"处理调度工作的方式。
    """
    while True:
        # 排出定时任务通知
        notifications = scheduler.drain_notifications()
        for note in notifications:
            print(f"[定时任务通知] {note[:100]}")
            messages.append({"role": "user", "content": note})

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

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    scheduler.start()
    print("[定时任务已运行。后台每秒检查一次。]")
    print("[命令：输入 /cron 查看任务，输入 /test 发送测试通知]")

    history = []
    while True:
        try:
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            scheduler.stop()
            break
        if query.strip().lower() in ("q", "exit", ""):
            scheduler.stop()
            break

        if query.strip() == "/cron":
            print(scheduler.list_tasks())
            continue

        if query.strip() == "/test":
            # 手动入队一条测试通知用于演示
            scheduler.queue.put("[定时任务 test-0000]：这是一条测试通知。")
            print("[测试通知已入队。它将在你的下一条消息中注入。]")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
