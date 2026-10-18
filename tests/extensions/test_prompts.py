import pytest

from minisweagent.extensions.config import PromptsConfig
from minisweagent.extensions.prompts import collect_prompt_paths, load_mounted_prompts


def test_load_files_and_dirs(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Workspace: ${WORKSPACE}", encoding="utf-8")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "b.md").write_text("Second", encoding="utf-8")
    (prompts / "a.md").write_text("First", encoding="utf-8")
    (prompts / "ignored.json").write_text("{}", encoding="utf-8")

    config = PromptsConfig(files=["AGENTS.md"], dirs=["prompts"])
    mounted, section = load_mounted_prompts(config, tmp_path)

    assert [item.path.name for item in mounted] == ["AGENTS.md", "a.md", "b.md"]
    assert str(tmp_path) in section
    assert section.startswith("<mounted_instructions>")
    assert section.rstrip().endswith("</mounted_instructions>")
    assert "First" in section and "Second" in section


def test_globs_and_dedupe(tmp_path):
    (tmp_path / "one.md").write_text("one", encoding="utf-8")
    (tmp_path / "two.md").write_text("two", encoding="utf-8")
    config = PromptsConfig(files=["*.md", "one.md"])
    mounted, _ = load_mounted_prompts(config, tmp_path)
    assert [item.path.name for item in mounted] == ["one.md", "two.md"]


def test_missing_optional_is_skipped(tmp_path):
    config = PromptsConfig(files=["missing.md"])
    mounted, section = load_mounted_prompts(config, tmp_path)
    assert mounted == []
    assert section == ""


def test_missing_required_raises(tmp_path):
    config = PromptsConfig(files=["missing.md"], required=True)
    with pytest.raises(FileNotFoundError):
        collect_prompt_paths(config, tmp_path)
