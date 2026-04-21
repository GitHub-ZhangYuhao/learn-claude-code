#!/usr/bin/env python3
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

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference")
MAX_INDEX_LINES = 200


class MemoryManager:
    """
    跨会话加载、构建并保存持久化记忆。

    这个教学版让记忆保持显式：
    每条记忆一个 Markdown 文件，加一个紧凑的索引文件。
    """

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memories = {}  # name -> {description, type, content}

    def load_all(self):
        """加载 MEMORY.md 索引和所有单独的记忆文件。"""
        self.memories = {}
        if not self.memory_dir.exists():
            return

        # 扫描除 MEMORY.md 之外的所有 .md 文件
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if parsed:
                name = parsed.get("name", md_file.stem)
                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": parsed.get("type", "project"),
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                }

        count = len(self.memories)
        if count > 0:
            print(f"[已加载记忆：来自 {self.memory_dir} 的 {count} 条记忆]")

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

        返回状态消息。
        """
        if mem_type not in MEMORY_TYPES:
            return f"错误：type 必须是 {MEMORY_TYPES} 之一"

        # 清理名称以用于文件名
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
        if not safe_name:
            return "错误：无效的记忆名称"

        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # 写入带前置信息的单独记忆文件
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"---\n"
            f"{content}\n"
        )
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(frontmatter, encoding="utf-8")

        # 更新内存中的存储
        self.memories[name] = {
            "description": description,
            "type": mem_type,
            "content": content,
            "file": file_name,
        }

        # 重建 MEMORY.md 索引
        self._rebuild_index()

        return f"已保存记忆 '{name}' [{mem_type}] 到 {file_path.relative_to(WORKDIR)}"

    def _rebuild_index(self):
        """根据当前内存状态重建 MEMORY.md，最多 200 行。"""
        lines = ["# Memory Index", ""]
        for name, mem in self.memories.items():
            lines.append(f"- {name}: {mem['description']} [{mem['type']}]")
            if len(lines) >= MAX_INDEX_LINES:
                lines.append(f"... (truncated at {MAX_INDEX_LINES} lines)")
                break
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _parse_frontmatter(self, text: str) -> dict | None:
        """解析以 --- 分隔的前置信息与正文内容。"""
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


class DreamConsolidator:
    """
    会话间的记忆自动合并（"Dream"）。

    这是一个可选的后期功能。它的职责是防止记忆库演变成嘈杂的堆栈，
    通过合并、去重和清理条目来保持质量。
    """

    COOLDOWN_SECONDS = 86400       # 两次合并之间冷却 24 小时
    SCAN_THROTTLE_SECONDS = 600    # 两次扫描尝试之间间隔 10 分钟
    MIN_SESSION_COUNT = 5          # 需要足够会话数据才能合并
    LOCK_STALE_SECONDS = 3600      # PID 锁 1 小时后视为过期

    PHASES = [
        "定向：扫描 MEMORY.md 索引的结构与分类",
        "收集：读取单独的记忆文件以获得完整内容",
        "合并：合并相关记忆，移除过期条目",
        "修剪：对 MEMORY.md 索引执行 200 行限制",
    ]

    def __init__(self, memory_dir: Path = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.lock_file = self.memory_dir / ".dream_lock"
        self.enabled = True
        self.mode = "default"
        self.last_consolidation_time = 0.0
        self.last_scan_time = 0.0
        self.session_count = 0

    def should_consolidate(self) -> tuple[bool, str]:
        """
        按顺序检查 7 道门槛，必须全部通过。
        返回 (can_run, reason)，reason 解释第一个失败的门槛。
        """
        import time

        now = time.time()

        # 门槛 1：启用标志
        if not self.enabled:
            return False, "Gate 1: 合并被禁用"

        # 门槛 2：记忆目录存在且有记忆文件
        if not self.memory_dir.exists():
            return False, "Gate 2: 记忆目录不存在"
        memory_files = list(self.memory_dir.glob("*.md"))
        # 从计数中排除 MEMORY.md
        memory_files = [f for f in memory_files if f.name != "MEMORY.md"]
        if not memory_files:
            return False, "Gate 2: 未找到记忆文件"

        # 门槛 3：不在 plan 模式（只在活跃模式下合并）
        if self.mode == "plan":
            return False, "Gate 3: plan 模式不允许合并"

        # 门槛 4：距离上次合并满 24 小时
        time_since_last = now - self.last_consolidation_time
        if time_since_last < self.COOLDOWN_SECONDS:
            remaining = int(self.COOLDOWN_SECONDS - time_since_last)
            return False, f"Gate 4: 冷却中，剩余 {remaining}s"

        # 门槛 5：距离上次扫描尝试满 10 分钟
        time_since_scan = now - self.last_scan_time
        if time_since_scan < self.SCAN_THROTTLE_SECONDS:
            remaining = int(self.SCAN_THROTTLE_SECONDS - time_since_scan)
            return False, f"Gate 5: 扫描节流中，剩余 {remaining}s"

        # 门槛 6：至少 5 个会话数据
        if self.session_count < self.MIN_SESSION_COUNT:
            return False, f"Gate 6: 仅 {self.session_count} 个会话，需要 {self.MIN_SESSION_COUNT}"

        # 门槛 7：没有活动锁文件（检查 PID 是否过期）
        if not self._acquire_lock():
            return False, "Gate 7: 锁被另一个进程持有"

        return True, "All 7 gates passed"

    def consolidate(self) -> list[str]:
        """
        运行 4 阶段合并流程。

        教学版返回阶段描述，以便无需额外 LLM 调用即可观察流程。
        """
        import time

        can_run, reason = self.should_consolidate()
        if not can_run:
            print(f"[Dream] 无法合并：{reason}")
            return []

        print("[Dream] 开始合并...")
        self.last_scan_time = time.time()

        completed_phases = []
        for i, phase in enumerate(self.PHASES, 1):
            print(f"[Dream] Phase {i}/4: {phase}")
            completed_phases.append(phase)

        self.last_consolidation_time = time.time()
        self._release_lock()
        print(f"[Dream] 合并完成：执行了 {len(completed_phases)} 个阶段")
        return completed_phases

    def _acquire_lock(self) -> bool:
        """
        获取基于 PID 的锁文件。若被另一个仍存活的进程持有则返回 False。
        过期锁（超过 LOCK_STALE_SECONDS）会被移除。
        """
        import time

        if self.lock_file.exists():
            try:
                lock_data = self.lock_file.read_text(encoding="utf-8").strip()
                pid_str, timestamp_str = lock_data.split(":", 1)
                pid = int(pid_str)
                lock_time = float(timestamp_str)

                # 检查锁是否过期
                if (time.time() - lock_time) > self.LOCK_STALE_SECONDS:
                    print(f"[Dream] 移除来自 PID {pid} 的过期锁")
                    self.lock_file.unlink()
                else:
                    # 检查持有锁的进程是否仍存活
                    try:
                        os.kill(pid, 0)
                        return False  # 进程仍存活，锁有效
                    except OSError:
                        print(f"[Dream] 移除来自已退出 PID {pid} 的锁")
                        self.lock_file.unlink()
            except (ValueError, OSError):
                # 锁文件损坏，移除
                self.lock_file.unlink(missing_ok=True)

        # 写入新锁
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self.lock_file.write_text(f"{os.getpid()}:{time.time()}", encoding="utf-8")
            return True
        except OSError:
            return False

    def _release_lock(self):
        """如果锁归我们所有，则释放锁文件。"""
        try:
            if self.lock_file.exists():
                lock_data = self.lock_file.read_text(encoding="utf-8").strip()
                pid_str = lock_data.split(":")[0]
                if int(pid_str) == os.getpid():
                    self.lock_file.unlink()
        except (ValueError, OSError):
            pass


# -- 工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：已阻止危险命令"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：超时（120s）"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
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
            return f"错误：在 {path} 中未找到文本"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


# 全局记忆管理器
memory_mgr = MemoryManager()


def run_save_memory(name: str, description: str, mem_type: str, content: str) -> str:
    return memory_mgr.save_memory(name, description, mem_type, content)


TOOL_HANDLERS = {
    "bash":         lambda **kw: run_bash(kw["command"]),
    "read_file":    lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":   lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":    lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "save_memory":  lambda **kw: run_save_memory(kw["name"], kw["description"], kw["type"], kw["content"]),
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "save_memory", "description": "Save a persistent memory that survives across sessions.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Short identifier (e.g. prefer_tabs, db_schema)"},
         "description": {"type": "string", "description": "One-line summary of what this memory captures"},
         "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                  "description": "user=preferences, feedback=corrections, project=non-obvious project conventions or decision reasons, reference=external resource pointers"},
         "content": {"type": "string", "description": "Full memory content (multi-line OK)"},
     }, "required": ["name", "description", "type", "content"]}},
]

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
    """组装系统提示，并包含记忆内容。"""
    parts = [f"你是位于 {WORKDIR} 的编码代理。请使用工具完成任务。"]

    # 如有可用记忆则注入记忆内容
    memory_section = memory_mgr.load_memory_prompt()
    if memory_section:
        parts.append(memory_section)

    parts.append(MEMORY_GUIDANCE)
    return "\n\n".join(parts)


def agent_loop(messages: list):
    """
    带记忆感知系统提示的代理循环。

    每次调用都会重建系统提示，使新保存的记忆
    在同一会话的下一次 LLM 轮次中可见。
    """
    while True:
        system = build_system_prompt()
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
    # 会话开始时加载已有记忆
    memory_mgr.load_all()
    mem_count = len(memory_mgr.memories)
    if mem_count:
        print(f"[已将 {mem_count} 条记忆加载到上下文中]")
    else:
        print("[暂无记忆。可通过 save_memory 创建记忆。]")

    history = []
    while True:
        try:
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        # /memories 命令：列出当前记忆
        if query.strip() == "/memories":
            if memory_mgr.memories:
                for name, mem in memory_mgr.memories.items():
                    print(f"  [{mem['type']}] {name}: {mem['description']}")
            else:
                print("  (无记忆)")
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
