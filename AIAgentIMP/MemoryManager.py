from datetime import datetime

from GlobalConfig import *

DEFAULT_TTL = {
    "user": None,       # 用户偏好永不过期
    "feedback": None,   # 反馈指导永不过期
    "project": 30,      # 项目信息30天后过期
    "reference": None,  # 外部链接引用永不过期
    "mistakes": None,   # 踩坑教训永不过期
    "glossary": None,   # 项目术语永不过期
    "workflows": None,  # 重复工作流永不过期
}

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
MEMORY_TYPES = ("user", "feedback", "project", "reference", "mistakes", "glossary", "workflows")
MAX_INDEX_LINES = 200



MEMORY_MANAGER_TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": "Save a cross-session memory. Use this to record user preferences, project decisions, mistakes, glossary terms, or workflows that should persist across conversations.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short identifier for the memory, e.g. 'user_role', 'auth_migration_decision'"
                },
                "description": {
                    "type": "string",
                    "description": "One-line summary of what this memory is about"
                },
                "mem_type": {
                    "type": "string",
                    "enum": ["user", "feedback", "project", "reference", "mistakes", "glossary", "workflows"],
                    "description": (
                        "Type of memory: "
                        "'user' for user preferences/background, "
                        "'feedback' for guidance the user gave, "
                        "'project' for ongoing work/decisions (expires in 30 days), "
                        "'reference' for external links/resources, "
                        "'mistakes' for lessons learned from bugs/failures, "
                        "'glossary' for project-specific terms, "
                        "'workflows' for repeatable processes"
                    )
                },
                "content": {
                    "type": "string",
                    "description": "Detailed memory content. For feedback/mistakes, include 'Why:' and 'How to apply:' lines."
                }
            },
            "required": ["name", "description", "mem_type", "content"],
            "additionalProperties": False
        }
    }
}]

class MemoryManager:
    """
    跨会话加载， 构建并保存持久记忆

    这个教学版让记忆保持显式：
    每条记忆一个 Markdown 文件， 加一个紧凑的索引文件
    """

    def __init__(self, memory_dir: Path = None, default_ttl: dict = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.default_ttl = default_ttl or DEFAULT_TTL  # type -> days, None=永不过期
        self.memories = {} # name -> {description, type, content, file, date}

    def load_all(self, skip_expired: bool = True, auto_cleanup: bool = False):
        """
        加载MEMORY.md 索引和所有单独的记忆文件

        Args:
            skip_expired: 是否跳过已过期的记忆（默认True）
            auto_cleanup: 是否自动删除过期文件（默认False）
        """
        self.memories = {}
        if not self.memory_dir.exists():
            return


        today = datetime.now()
        skipped = 0
        cleaned = 0

        #扫描除 MEMORY.md 之外的所有的 .md 文件
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if parsed:
                name = parsed.get("name", md_file.stem)
                mem_type = parsed.get("type", "project")

                # 检查是否过期
                date_str = parsed.get("date", "")
                mem_date = self._parse_date(date_str) if date_str else None
                if skip_expired and mem_date and self._is_expired(mem_type, mem_date, today):
                    if auto_cleanup:
                        md_file.unlink()
                        cleaned += 1
                        continue
                    skipped += 1
                    continue

                self.memories[name] = {
                    "description": parsed.get("description", ""),
                    "type": mem_type,
                    "content": parsed.get("content", ""),
                    "file": md_file.name,
                    "date": date_str,
                }

        count = len(self.memories)
        msg = f"[已加载记忆： 来自{self.memory_dir} 的 {count} 条记忆]"
        if cleaned > 0:
            msg += f" (自动清理 {cleaned} 条过期记忆)"
        elif skipped > 0:
            msg += f" (跳过 {skipped} 条过期记忆)"
        if cleaned > 0:
            self._rebuild_index()
        print(msg)

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

        today = datetime.now().strftime("%Y-%m-%d")  # '2026-05-10'

        # 写入带强制信息的单独记忆文件
        frontmatter = (
            f"---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"type: {mem_type}\n"
            f"date: {today}\n"
            f"---\n"
            f"{content}\n"
        )
        #将记忆写入磁盘
        file_name = f"{safe_name}.md"
        file_path = self.memory_dir / file_name
        file_path.write_text(frontmatter, encoding="utf-8")

        # 更新内存中的记忆存储
        self.memories[name] = {
            "description": description,
            "type": mem_type,
            "content": content,
            "file": file_name,
            "date": today,
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

    # --- 过期相关 ---

    @staticmethod
    def _parse_date(date_str: str):
        """解析日期字符串为 date 对象，解析失败返回 None。"""
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except (ValueError, TypeError):
                continue
        return None

    def _is_expired(self, mem_type: str, mem_date, today) -> bool:
        """判断某条记忆是否已过期。"""
        ttl_days = self.default_ttl.get(mem_type)
        if ttl_days is None:  # None = 永不过期
            return False
        age_days = (today - mem_date).days
        return age_days > ttl_days

    def cleanup_expired_memories(self, days: int = None, dry_run: bool = True) -> list:
        """
        清理过期记忆文件。

        Args:
            days: 全局过期天数，None 则使用 default_ttl 配置
            dry_run: 是否只预览不删除

        Returns:
            被清理（或标记清理）的文件路径列表
        """
        today = datetime.now().date()
        removed = []

        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            parsed = self._parse_frontmatter(md_file.read_text(encoding="utf-8"))
            if not parsed:
                continue

            mem_type = parsed.get("type", "project")
            date_str = parsed.get("date", "")
            mem_date = self._parse_date(date_str) if date_str else None
            if not mem_date:
                continue

            ttl = days if days is not None else self.default_ttl.get(mem_type)
            if ttl is None:
                continue

            age = (today - mem_date).days
            if age > ttl:
                if dry_run:
                    removed.append(f"[预览删除] {md_file.name} ({mem_type}, {age}天前)")
                else:
                    md_file.unlink()
                    removed.append(f"[已删除] {md_file.name} ({mem_type}, {age}天前)")
                    name = parsed.get("name", md_file.stem)
                    self.memories.pop(name, None)

        if removed:
            action = "预览" if dry_run else "已清理"
            print(f"[{action}] {len(removed)} 条过期记忆:")
            for line in removed:
                print(f"  {line}")
            if not dry_run:
                self._rebuild_index()
        else:
            print("[无过期记忆]")

        return removed

    def set_ttl(self, mem_type: str, days: int):
        """修改某类记忆的默认过期天数，days=None 表示永不过期。"""
        if mem_type not in MEMORY_TYPES:
            raise ValueError(f"type 必须是 {MEMORY_TYPES} 之一")
        self.default_ttl[mem_type] = days

    def list_ttl_config(self) -> dict:
        """返回当前各类记忆的过期配置。"""
        return dict(self.default_ttl)

# 初始化 Memory Manager
_MEMORY_MANAGER = MemoryManager()
_MEMORY_MANAGER.load_all()
MEMORY_SAVE_MEMORY_TOOL_HANDLERS = {"save_memory":  lambda **kw: _MEMORY_MANAGER.save_memory(kw["name"], kw["description"], kw["mem_type"], kw["content"]),}

if __name__ == "__main__":
    pass
