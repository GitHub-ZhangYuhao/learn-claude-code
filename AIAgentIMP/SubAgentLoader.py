import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from GlobalConfig import *




@dataclass
class SubAgentConfig:
    """子Agent配置，从 .agent/*.md 文件解析而来。"""
    name: str
    description: str
    skills: list = field(default_factory=list)
    detail: str = ""  # frontmatter 之后的 body 内容


class SubAgentLoader:
    """加载并解析 .agent 文件夹下的子Agent .md 配置文件。"""

    def __init__(self, workdir: Optional[Path] = None):
        self.workdir = workdir or WORKDIR
        self.agent_dir = SUB_AGENT_DIR
        self._cache: dict[str, SubAgentConfig] = {}
        self.load()

    def load(self) -> dict[str, SubAgentConfig]:
        """扫描 .agent 目录，解析所有 .md 文件，返回 name -> SubAgentConfig 映射。"""
        if not self.agent_dir.exists():
            self.agent_dir.mkdir(exist_ok=True)
            return {}

        result = {}
        for md_file in sorted(self.agent_dir.glob("*.md")):
            config = self._parse_file(md_file)
            if config:
                result[config.name] = config
        self._cache = result
        return result

    def get(self, name: str) -> Optional[SubAgentConfig]:
        """按名称获取已加载的子Agent配置。"""
        return self._cache.get(name)

    def list_names(self) -> list[str]:
        """返回所有已加载的子Agent名称。"""
        return list(self._cache.keys())

    def _parse_file(self, file_path: Path) -> Optional[SubAgentConfig]:
        """解析单个 .md 文件，提取 frontmatter 和指令正文。"""
        text = file_path.read_text(encoding="utf-8")

        # 匹配 frontmatter: ---\n...\n---
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", text, re.DOTALL)
        if not match:
            print(f"[SubAgentLoader] 警告: {file_path.name} 缺少 frontmatter，跳过")
            return None

        head, body = match.group(1), match.group(2).strip()

        # 解析 frontmatter 中的 key: value
        meta = {}
        for line in head.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()

        # skills 字段可能是 YAML 列表，简单解析
        skills = []
        skills_raw = meta.get("skills", "[]")
        if skills_raw.startswith("[") and skills_raw.endswith("]"):
            inner = skills_raw[1:-1].strip()
            if inner:
                skills = [s.strip().strip('"').strip("'") for s in inner.split(",")]

        agent_name = meta.get("name", file_path.stem)
        agent_description = meta.get("description", "")

        return SubAgentConfig(
            name=agent_name,
            description=agent_description,
            skills=skills,
            detail=body,
        )


if __name__ == "__main__":

    from TeammateManager import *

    _SubAgentLoader = SubAgentLoader()

    tm = TeammateManager()

    for name, config in _SubAgentLoader.load().items():
        tm.spawn(
            name=config.name,
            role=config.description,
            skills=config.skills,
            agent_detail=config.detail,
        )

    print(_SubAgentLoader.list_names())

    while True:
        sleep(3)
        print(tm.list_all())
