from pathlib import Path

from minisweagent.context import ContextBuilder
from minisweagent.extensions.config import ContextConfig, SkillsConfig
from minisweagent.extensions.skills import discover_skills


def make_skills(tmp_path: Path, count: int = 8):
    for index in range(count):
        directory = tmp_path / "skills" / f"skill{index}"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: skill{index}\ndescription: {'description ' * 20}\n---\nbody",
            encoding="utf-8",
        )
    return discover_skills(SkillsConfig(enabled=True, dirs=["skills"]), tmp_path)


def make_builder(tmp_path, config: ContextConfig, skills=None):
    return ContextBuilder(
        config,
        base_prompt="BASE",
        workspace=tmp_path,
        model_name="test-model",
        skills=skills or {},
    )


def test_skills_use_compact_form_when_over_budget(tmp_path):
    skills = make_skills(tmp_path)
    constrained = make_builder(
        tmp_path,
        ContextConfig(enabled=True, section_budgets={"skills": 120}, env_info=False, current_date=False),
        skills,
    )
    result = constrained.build()
    skills_section = next(section for section in result.sections if section.source == "skills")
    assert "The following skills are available" in skills_section.content
    assert "(file:" in skills_section.content
    assert "description description" not in skills_section.content

    generous = make_builder(
        tmp_path,
        ContextConfig(enabled=True, section_budgets={"skills": 20_000}, env_info=False, current_date=False),
        skills,
    ).build()
    generous_section = next(section for section in generous.sections if section.source == "skills")
    assert "description description" in generous_section.content


def test_meta_user_sections_are_not_in_system_prompt(tmp_path):
    skills = make_skills(tmp_path, count=2)
    result = make_builder(
        tmp_path,
        ContextConfig(enabled=True, env_info=False, current_date=False),
        skills,
    ).build()
    assert "BASE" in result.system_content
    assert "The following skills are available" not in result.system_content
    assert "The following skills are available" in result.meta_user_content
    assert "IMPORTANT: this context may or may not be relevant" in result.meta_user_content


def test_total_budget_drops_low_priority_sections(tmp_path):
    skills = make_skills(tmp_path, count=20)
    result = make_builder(
        tmp_path,
        ContextConfig(
            enabled=True,
            max_total_tokens=200,
            max_section_tokens=200,
            section_budgets={"skills": 200},
            env_info=True,
            current_date=True,
        ),
        skills,
    ).build()
    assert "BASE" in result.system_content
    assert result.total_tokens <= 260  # base is always kept, plus a small overshoot
    assert result.dropped or "The following skills" not in result.system_content


def test_reminders_have_wrapper_and_budget(tmp_path):
    class Todo:
        items = [type("Item", (), {"line": lambda self: "[pending] (high) do it"})()]

        def format(self):
            return "Todos (0/1 completed):\n- [pending] (high) do it"

    class State:
        summary = "checkpoint text"

    builder = make_builder(
        tmp_path,
        ContextConfig(enabled=True, system_reminders=True, reminder_tokens=500, env_info=False, current_date=False),
    )
    sections = builder.build_reminders(Todo(), State(), microcompacted=True)
    assert any(section.source == "todo" for section in sections)
    assert any(section.source == "compaction" for section in sections)
    assert any(section.source == "microcompact" for section in sections)
    content = builder.reminder_content(sections)
    assert content.startswith("<system-reminder>")
    assert content.rstrip().endswith("</system-reminder>")
    assert "checkpoint text" in content
