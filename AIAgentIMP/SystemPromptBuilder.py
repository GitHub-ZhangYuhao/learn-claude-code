from GlobalConfig import *

DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="
class SystemPromptBuilder:
    """
    将系统提示词由独立的模块组合而成。
    此处的设计宗旨在于清晰明确：
    每个模块对应一个来源，承担一项职责。
    这使得提示词更易于梳理逻辑、更便于测试，并且在智能体拓展新功能时，也更易于迭代优化。
    """

    def __init__(self, workdir: Path = None, tools : list = None):
        self.workdir = workdir or WORKDIR
        self.tools = tools or []
        self.skills_dir = self.workdir / "skills"
        self.memory_dir = self.workdir / ".memory"

    # -- 第1节:核心指令 --
    def _build_core(self) -> str:
        return (
            f"You are a coding agent operating in {self.workdir}.\n"
            "使用提供的工具来探索、读取、写入和编辑文件。\n"
            "在操作前务必验证，不要凭空猜测。优先读取文件而非猜测内容。"
        )

    # -- 第2节:工具列表 --
    def _build_tool_listing(self) -> str:
        if not self.tools:
            return ""
        lines = ["# 可用工具"]
        for tool in self.tools:
            """
            工具列表项
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            """
            props = tool.get("input_schema", {}).get("properties", {})  # "properties": {"command": {"type": "string"}},
            params = ", ".join(props.keys())
            lines.append(f"- {tool['name']}:({params}): {tool['description']}")
        return  "\n".join(lines)

    # -- 第3节:技能元数据 --
    def _build_skill_listing(self) -> str:
        if not self.skills_dir.exists():
            return ""
        skills = []
        for skill_dir in sorted(self.skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            text = skill_md.read_text(encoding="UTF-8")
            # 解析 frontmatter 获取名称和描述
            match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if not match:
                continue
            meta = {}
            for line in match.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            name = meta.get("name", skill_dir.name)
            desc = meta.get("description", "")
            skills.append(f" - {name}: {desc}")
        if not skills:
            return ""
        return "# 可用技能\n" + "\n".join(skills)

    def _build_memory_section(self) -> str:
        if not self.memory_dir.exists():
            return ""
        memories = []
        for md_file in sorted(self.memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            text = md_file.read_text(encoding="utf-8")
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
            if not match:
                continue
            head, body = match.group(1), match.group(2).strip()
            meta = {}
            for line in head.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
            name = meta.get("name", md_file.stem)
            mem_type = meta.get("type", "project")
            desc = meta.get("description", "")
            memories.append(f"[{mem_type}] {name}: {desc}\n{body}")
        if not memories:
            return ""
        return "# 记忆（持久化）\n\n" + "\n\n".join(memories)

    def _build_claude_md(self) -> str:
        """
        按优先级顺序加载 CLAUDE.md 文件（全部包含）：
        1. ~/.claude/CLAUDE.md（用户全局指令）
        2. <项目根目录>/CLAUDE.md（项目指令）
        3. <当前子目录>/CLAUDE.md（目录特定指令）
        """
        sources = []

        # 用户全局
        user_claude = Path.home() / ".claude" / "CLAUDE.md"
        if user_claude.exists():
            sources.append(("用户全局 (~/.claude/CLAUDE.md)", user_claude.read_text(encoding="utf-8")))

        # 项目根目录
        project_claude = self.workdir / "CLAUDE.md"
        if project_claude.exists():
            sources.append(("项目根目录 (CLAUDE.md)", project_claude.read_text(encoding="utf-8")))

        # 子目录 -- 在真实 CC 中，这会从 cwd 线上遍历到项目根目录
        # 教学： 如果 cwd 不同于 workdir， 则检查 cwd
        cwd = Path.cwd()
        if cwd != self.workdir:
            subdir_claude = cwd / "CLAUDE.md"
            if subdir_claude.exists():
                sources.append((f"子目录 ({cwd.name}/CLAUDE.md)", subdir_claude.read_text(encoding="utf-8")))

        if not sources:
            return ""
        parts = ["# CLAUDE.md 指令"]
        for label, content in sources:
            parts.append(f"## 来自{label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    def _build_dynamic_context(self) -> str:
        lines = [
            f"当前日期：{datetime.date.today().isoformat()}"
            f"工作目录：{self.workdir}"
            f"模型：{MODEL}"
            f"平台：{platform.system()}"
        ]
        return "# 动态上下文\n" + "\n".join(lines)

    def build(self) -> str:
        """
        从所有段落拼装完整的系统提示词。

        静态段落（1-5）与动态段落（6）之间通过
        DYNAMIC_BOUNDARY 标记分隔。在真实 CC 中，静态前缀
        会在轮次间缓存以节省提示词 token。
        """
        sections = []

        core = self._build_core()
        if core:
            sections.append(core)

        tools = self._build_tool_listing()
        if tools:
            sections.append(tools)

        skills = self._build_skill_listing()
        if skills:
            sections.append(skills)

        memory = self._build_memory_section()
        if memory:
            sections.append(memory)

        claude_md = self._build_claude_md()
        if claude_md:
            sections.append(claude_md)

        # 静态/动态边界
        sections.append(DYNAMIC_BOUNDARY)

        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)

        return "\n\n".join(sections)
