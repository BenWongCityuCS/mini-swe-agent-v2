from minisweagent.extensions.config import SkillsConfig
from minisweagent.extensions.skills import (
    build_skill_tool,
    discover_skills,
    format_skills_index,
    parse_frontmatter,
)


def write_skill(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_frontmatter():
    meta, body = parse_frontmatter("---\nname: x\ndescription: y\n---\nBody")
    assert meta == {"name": "x", "description": "y"}
    assert body == "Body"


def test_discover_directory_and_single_file_skills(tmp_path):
    skills_root = tmp_path / "skills"
    write_skill(
        skills_root,
        "pdf/SKILL.md",
        "---\n"
        "name: pdf-tools\n"
        "description: Work with PDF files\n"
        "when_to_use: when a task mentions a PDF\n"
        "aliases: [pdf]\n"
        "---\n"
        "Use the pdf CLI.\n"
        "Skill dir: ${SKILL_DIR}\n",
    )
    write_skill(
        skills_root,
        "quick.md",
        "---\n"
        "name: quick\n"
        "description: Quick helper\n"
        "---\n"
        "Do the quick thing.\n",
    )

    config = SkillsConfig(enabled=True, dirs=["skills"])
    skills = discover_skills(config, tmp_path)

    assert {"pdf-tools", "pdf", "quick"} <= set(skills)
    index = format_skills_index(skills, config)
    assert "- pdf-tools: Work with PDF files - when a task mentions a PDF" in index
    assert "(also loadable as pdf)" in index
    assert "- quick: Quick helper" in index

    tool = build_skill_tool(skills, config, tmp_path)
    result = tool.handler({"name": "pdf"})
    assert result["returncode"] == 0
    assert '<skill_content name="pdf-tools">' in result["output"]
    assert str(skills_root / "pdf") in result["output"]
    assert "${SKILL_DIR}" not in result["output"]


def test_later_directory_overrides(tmp_path):
    write_skill(tmp_path, "one/demo/SKILL.md", "---\nname: demo\ndescription: first\n---\nfirst")
    write_skill(tmp_path, "two/demo/SKILL.md", "---\nname: demo\ndescription: second\n---\nsecond")
    config = SkillsConfig(enabled=True, dirs=["one", "two"])
    skills = discover_skills(config, tmp_path)
    assert skills["demo"].description == "second"
    assert skills["demo"].body == "second"


def test_unknown_skill_and_truncation(tmp_path):
    write_skill(tmp_path, "big/SKILL.md", "---\nname: big\ndescription: big\n---\n" + "x" * 500)
    config = SkillsConfig(enabled=True, dirs=["."], max_bytes=100)
    skills = discover_skills(config, tmp_path)
    tool = build_skill_tool(skills, config, tmp_path)

    missing = tool.handler({"name": "nope"})
    assert missing["returncode"] == -1
    assert "Unknown skill" in missing["exception_info"]

    truncated = tool.handler({"name": "big"})
    assert "[Skill content truncated]" in truncated["output"]
