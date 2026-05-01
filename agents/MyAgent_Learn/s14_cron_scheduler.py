# Harness: 后台执行 —— 模型在思考的同时，harness 等待后台结果。


import datetime
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 初始化模型和工作区
WORKDIR = Path.cwd()
RUNTIME_DIR = WORKDIR / ".runtime-tasks"
RUNTIME_DIR.mkdir(exist_ok=True)
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SCHEDULED_TASKS_FILE = WORKDIR / ".claude" / "scheduled_tasks.json"
AUTO_EXPIRY_DAYS = 7
JITTER_MINUTES = [0, 30]  # 对重复任务避开这些精确分钟
JITTER_OFFSET_MAX = 4     # 偏移范围（分钟）
# 教学版本：需要时使用 1-4 分钟的偏移。

SYSTEM = f"你是一个编码代理，工作目录为 {WORKDIR}。使用 background_run 执行长时间运行的命令。"

STALL_THRESHOLD_S = 45  # 任务被视为停滞的秒数

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
    def __init__(self):
        self.tasks = []         # 任务字典列表
        self.queue = Queue()    # 通知队列，queue 是线程安全的通信方式。
        self._stop_event = threading.Event()
        self._thread = None
        self._last_check_minute = -1  # 避免同一分钟内重复触发

    def start(self):
        """加载持久化任务并启动后台检查线程。"""
        self._load_durable()
        self._thread = threading.Thread(target=self._check_loop, daemon=True, name="Background_Thread")
        self._thread.start()
        count = len(self.tasks)
        if count:
            print(f"[定时任务] 加载了 {count} 个任务")

    def stop(self):
        """停止后台线程"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def create(self, cron_expr: str, prompt: str,
               recurring: bool = True, durable: bool = False) -> str:
        """创建新的定时任务，返回任务ID"""
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        task = {
            "id": task_id,
            "cron": cron_expr,
            "prompt": prompt,
            "recurring": recurring,
            "durable": durable,
            "createdAt":now,
        }

        # 抖动： 对于重复任务，如果 cron 在 :00 或 :30 触发，
        # 记录偏移以便稍作延迟
        if recurring:
            task["jitter_offset"] = self._compute_jitter(cron_expr)

        self.tasks.append(task)
        if durable:
            self._save_durable()

        mode = "重复" if recurring else "单次"
        store = "持久化" if durable else "仅会话"
        return f"已创建任务 {task_id} （{mode},{store}）: cron = {cron_expr}"

    def delete(self, task_id: str) -> str:
        """按照ID删除一个定时任务"""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t['id'] != task_id]
        if len(self.tasks) <= before:
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

            # 每分钟检查一次， 避免重复触发
            if current_minute != self._last_check_minute:
                self._last_check_minute = current_minute
                self._check_tasks(now)

            # 等待1秒，避免CPU占用过高
            self._stop_event.wait(timeout=1)

    def _check_tasks(self, now: datetime.datetime):
        """检查所有任务，触发到期任务"""
        expired = []
        fired_oneshots = []

        for task in self.tasks:
            # 自动过期：超过 7 天的任务
            age_days = (time.time() - task["createdAt"]) / 86400
            if task["recurring"] and age_days > AUTO_EXPIRY_DAYS:
                expired.append(task["id"])
                continue

            # 对匹配检查应用抖动偏移
            check_time = now
            jitter = task.get("jitter_offset", 0)
            if jitter:
                check_time = now - timedelta(seconds=jitter)

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
                print(f"[定时任务] 自动过期： {tid} (超过 {AUTO_EXPIRY_DAYS} 天)")
            for tid in fired_oneshots:
                print(f"[定时任务] 单次任务已完成并移除：{tid}")
            self._save_durable()

    #加载持久化任务到 tasks
    def _load_durable(self):
        """从.claude/scheduled_tasks.json 加载持久化任务。"""
        if not SCHEDULED_TASKS_FILE.exists():
            return
        try:
            data = json.loads(SCHEDULED_TASKS_FILE.read_text(encoding="utf-8"))
            # 只加载持久化任务
            self.tasks = [t for t in data if t.get("durable")]
        except Exception as e:
            print(f"[定时任务] 加载持久化任务失败: {e}")

    def _save_durable(self):
        """将任务持久保存到磁盘。"""
        durable = [t for t in self.tasks if t.get("durable")]
        SCHEDULED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCHEDULED_TASKS_FILE.write_text(
            json.dumps(durable, indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8"
        )

#全局定时调度器
scheduler = CronScheduler()

# TODO: 完成工具和 Agent_loop 方法的部分


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
    "bash":             lambda **kw : run_bash(kw["command"]),
    "read_file":        lambda **kw : run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw : run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw : run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # 定时调度工具
    "cron_create": lambda **kw: scheduler.create(
        kw["cron"], kw["prompt"], kw.get("recurring", True), kw.get("durable", False)),
    "cron_delete": lambda **kw: scheduler.delete(kw["id"]),
    "cron_list":   lambda **kw: scheduler.list_tasks(),
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
    # 定时调度工具 ToolSchema
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
    scheduler.start()
    print("[定时任务已运行，后台每秒检查一次]")
    print("[命令：输入 /cron 查看任务， 输入 /test 发送测试通知]")

    history = []
    while True:
        try:
            query = input("\033[36m 用户： >> \033[0m")    # \033[36m：ANSI转义序列，设置文本颜色为青色 ， \033[0m：ANSI转义序列，重置文本格式为默认状态
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        #定时调度 调试命令
        if query.strip() == "/cron":
            print(scheduler.list_tasks())
            continue
        if query.strip() == "/test":
            # 手动入队一条测试通知用于演示
            scheduler.queue.put("[定时任务 test-0000]:这是一条测试通知")
            print("[测试通知已入队。它将在你的吓一跳消息中注入。]")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)

        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()
