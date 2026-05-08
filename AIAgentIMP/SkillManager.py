from pathlib import Path
from dataclasses import dataclass
import re
from GlobalConfig import *

@dataclass
class SkillManifest:
    name: str
    description: str
    path: Path


@dataclass
class SkillDocument:
    manifest: SkillManifest
    body: str


class SkillRegistry:
    def __init__(self, skills_dir: Path, skill_allow_list: list[str] | None = None):
        self.skills_dir = skills_dir
        self.skill_allow_list = None if skill_allow_list is None else set(skill_allow_list)     #skills 白名单
        self.documents: dict[str, SkillDocument] = {}
        self._load_all()

    def _is_allowed(self, name: str) -> bool:
        if self.skill_allow_list is None:
            return True
        return name in self.skill_allow_list

    def _load_all(self) -> None:
        if not self.skills_dir.exists():
            return

        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            meta, body = self._parse_frontmatter(path.read_text(encoding="utf-8"))
            name = meta.get("name", path.parent.name)
            #skills 白名单检测，通过了才能加载技能
            if not self._is_allowed(name):
                continue
            description = meta.get("description", "No description")
            manifest = SkillManifest(name=name, description=description, path=path)
            self.documents[name] = SkillDocument(manifest=manifest, body=body.strip())

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text

        meta = {}
        for line in match.group(1).strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
        return meta, match.group(2)

    def describe_available(self) -> str:
        if not self.documents:
            return "(no skills available)"
        lines = []
        for name in sorted(self.documents):
            manifest = self.documents[name].manifest
            lines.append(f"- 技能名:[{manifest.name}]\n 路径:({manifest.path})\n 技能描述: {manifest.description}")
        return "\n".join(lines)

    def load_full_text(self, name: str) -> str:
        document = self.documents.get(name)
        if not document:
            known = ", ".join(sorted(self.documents)) or "(none)"
            return f"Error: Unknown skill '{name}'. Available skills: {known}"

        return (
            f"<skill name=\"{document.manifest.name}\">\n"
            f"{document.body}\n"
            "</skill>"
        )

if __name__ == "__main__":
    _MainAgent_Skills = SkillRegistry(SKILLS_DIR, ["pdf", "yh-test"])

    from SystemPromptBuilder import SystemPromptBuilder
    system_prompt_builder = SystemPromptBuilder(skill_registry=_MainAgent_Skills)

    print(system_prompt_builder.build())
