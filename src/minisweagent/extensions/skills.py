"""SKILL.md discovery and the ``Skill`` tool.

A skill is either ``<dir>/<name>/SKILL.md`` or ``<dir>/<name>.md`` with optional
YAML frontmatter::

    ---
    name: pdf-tools
    description: Work with PDF files
    when_to_use: when a task involves generating or reading a PDF
    aliases: [pdf]
    ---
    Full instructions, relative paths are resolved against the skill directory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from minisweagent.extensions.config import SkillsConfig
from minisweagent.extensions.registry import Tool, err, ok

logger = logging.getLogger("extensions")

_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)\Z", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    base_dir: Path
    when_to_use: str = ""
    aliases: list[str] = field(default_factory=list)

    def line(self, max_description_chars: int) -> str:
        description = self.description
        if self.when_to_use:
            description = f"{description} - {self.when_to_use}" if description else self.when_to_use
        if len(description) > max_description_chars:
            description = description[: max_description_chars - 1] + "…"
        suffix = f" (also loadable as {', '.join(self.aliases)})" if self.aliases else ""
        return f"- {self.name}: {description}{suffix} (file: {self.path})"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.warning("invalid skill frontmatter: %s", exc)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, match.group(2)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def load_skill(path: Path, default_name: str, base_dir: Path) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read skill %s: %s", path, exc)
        return None
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or default_name).strip()
    if not name:
        return None
    description = str(meta.get("description") or "").strip()
    if not description:
        for line in body.splitlines():
            if line.strip():
                description = line.strip()
                break
    when_to_use = str(
        meta.get("when_to_use") or meta.get("when-to-use") or meta.get("whenToUse") or ""
    ).strip()
    aliases = _as_list(meta.get("aliases") or meta.get("alias"))
    return Skill(
        name=name,
        description=description,
        body=body.strip(),
        path=path.resolve(),
        base_dir=base_dir.resolve(),
        when_to_use=when_to_use,
        aliases=aliases,
    )


def _resolve_dir(spec: str, workspace: Path) -> Path:
    path = Path(spec).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def discover_skills(config: SkillsConfig, workspace: Path) -> dict[str, Skill]:
    """Return skills keyed by name. Later directories override earlier ones."""
    skills: dict[str, Skill] = {}
    for spec in config.dirs:
        base = _resolve_dir(spec, workspace)
        if not base.is_dir():
            logger.debug("skill directory not found: %s", base)
            continue
        candidates: list[tuple[Path, str, Path]] = []
        for skill_md in sorted(base.rglob("SKILL.md")):
            candidates.append((skill_md, skill_md.parent.name, skill_md.parent))
        for md in sorted(base.glob("*.md")):
            if md.name == "SKILL.md":
                continue
            candidates.append((md, md.stem, base))
        for path, default_name, base_dir in candidates:
            skill = load_skill(path, default_name, base_dir)
            if skill is None:
                continue
            skills[skill.name] = skill
            for alias in skill.aliases:
                skills.setdefault(alias, skill)
    return skills


def format_skills_index(skills: dict[str, Skill], config: SkillsConfig) -> str:
    if not skills:
        return ""
    unique = {skill.name: skill for skill in skills.values()}
    ordered = [unique[name] for name in sorted(unique, key=str.lower)]
    lines = [
        "The following skills are available for use with the Skill tool:",
        "",
    ]
    full = "\n".join(lines + [skill.line(config.max_description_chars) for skill in ordered])
    if len(full) <= config.max_index_chars:
        return full
    names_only = [f"- {skill.name} (file: {skill.path})" for skill in ordered]
    return "\n".join(lines + names_only)


def _truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def build_skill_tool(skills: dict[str, Skill], config: SkillsConfig, workspace: Path) -> Tool:
    unique = {skill.name: skill for skill in skills.values()}
    available = ", ".join(sorted(unique, key=str.lower))

    def handler(args: dict) -> dict:
        requested = str(args.get("name") or "").strip()
        if not requested:
            return err("Skill name is required")
        skill = skills.get(requested)
        if skill is None:
            return err(f"Unknown skill {requested!r}. Available skills: {available or '(none)'}")
        body = skill.body
        for token in ("${SKILL_DIR}", "${ZCODE_SKILL_DIR}", "${SKILL_BASE_DIR}"):
            body = body.replace(token, str(skill.base_dir))
        body = body.replace("${WORKSPACE}", str(workspace))
        body, truncated = _truncate_bytes(body, config.max_bytes)
        parts = [
            f'<skill_content name="{skill.name}">',
            f"# Skill: {skill.name}",
            "",
            body,
            "",
            f"Base directory for this skill: {skill.base_dir}",
            "Relative paths in this skill are relative to this base directory.",
        ]
        if truncated:
            parts.append("[Skill content truncated]")
        parts.append("</skill_content>")
        return ok("\n".join(parts))

    return Tool(
        name="Skill",
        description=(
            "Load a skill's full instructions into the current conversation. "
            "Use this when the task matches one of the skills listed in the system prompt. "
            "Pass the exact skill name (no leading slash)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact skill name from the available skills list"}
            },
            "required": ["name"],
        },
        handler=handler,
        read_only=True,
    )
