"""Mount external Markdown/text files into the agent's system prompt."""

from __future__ import annotations

import glob
import logging
from dataclasses import dataclass
from pathlib import Path

from minisweagent.extensions.config import PromptsConfig

logger = logging.getLogger("extensions")


@dataclass
class MountedPrompt:
    path: Path
    content: str


def _resolve(spec: str, workspace: Path) -> list[Path]:
    expanded = Path(spec).expanduser()
    if not expanded.is_absolute():
        expanded = workspace / expanded
    # Glob only when the spec actually contains a wildcard.
    if any(ch in str(expanded) for ch in "*?["):
        return [Path(p) for p in sorted(glob.glob(str(expanded), recursive=True))]
    return [expanded]


def collect_prompt_paths(config: PromptsConfig, workspace: Path) -> list[Path]:
    paths: list[Path] = []
    for spec in config.files:
        found = _resolve(spec, workspace)
        if not found or not any(p.is_file() for p in found):
            if config.required:
                raise FileNotFoundError(f"Mounted prompt file not found: {spec}")
            logger.warning("mounted prompt file not found: %s", spec)
            continue
        paths.extend(p for p in found if p.is_file())

    for spec in config.dirs:
        for base in _resolve(spec, workspace):
            if not base.is_dir():
                if config.required:
                    raise FileNotFoundError(f"Mounted prompt directory not found: {spec}")
                logger.warning("mounted prompt directory not found: %s", spec)
                continue
            paths.extend(sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in {".md", ".txt"}))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def load_mounted_prompts(config: PromptsConfig, workspace: Path) -> tuple[list[MountedPrompt], str]:
    """Return the prompt files and one ready-to-append system-prompt section."""
    mounted: list[MountedPrompt] = []
    for path in collect_prompt_paths(config, workspace):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            if config.required:
                raise
            logger.warning("could not read mounted prompt %s: %s", path, exc)
            continue
        content = content.replace("${WORKSPACE}", str(workspace))
        mounted.append(MountedPrompt(path=path, content=content.rstrip()))

    if not mounted:
        return [], ""

    body = "\n\n".join(f"<!-- {prompt.path} -->\n{prompt.content}" for prompt in mounted)
    section = f"<{config.tag}>\n{config.header}\n\n{body}\n</{config.tag}>"
    return mounted, section
