import time
from pathlib import Path

from minisweagent.extensions.edit_tools import FileTracker, build_edit_tools
from minisweagent.extensions.tools import build_read_only_tools


def tools_for(workspace: Path):
    tracker = FileTracker()
    read = {tool.name: tool for tool in build_read_only_tools(workspace, tracker)}
    edit = {tool.name: tool for tool in build_edit_tools(workspace, tracker)}
    return tracker, {**read, **edit}


def test_edit_requires_read_first(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello world", encoding="utf-8")
    _, tools = tools_for(tmp_path)

    refused = tools["Edit"].handler(
        {"file_path": "a.txt", "old_string": "hello", "new_string": "goodbye"}
    )
    assert refused["returncode"] == -1
    assert "has not been read" in refused["exception_info"]

    read = tools["Read"].handler({"file_path": "a.txt"})
    assert read["returncode"] == 0
    edited = tools["Edit"].handler(
        {"file_path": "a.txt", "old_string": "hello", "new_string": "goodbye"}
    )
    assert edited["returncode"] == 0
    assert path.read_text(encoding="utf-8") == "goodbye world"


def test_edit_detects_external_change(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("one", encoding="utf-8")
    _, tools = tools_for(tmp_path)
    tools["Read"].handler({"file_path": "a.txt"})
    time.sleep(0.01)
    path.write_text("changed externally", encoding="utf-8")

    stale = tools["Edit"].handler({"file_path": "a.txt", "old_string": "one", "new_string": "two"})
    assert stale["returncode"] == -1
    assert "changed on disk" in stale["exception_info"]


def test_edit_requires_unique_match_unless_replace_all(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x x", encoding="utf-8")
    _, tools = tools_for(tmp_path)
    tools["Read"].handler({"file_path": "a.txt"})

    ambiguous = tools["Edit"].handler({"file_path": "a.txt", "old_string": "x", "new_string": "y"})
    assert ambiguous["returncode"] == -1
    assert "occurs 2 times" in ambiguous["exception_info"]

    replaced = tools["Edit"].handler(
        {"file_path": "a.txt", "old_string": "x", "new_string": "y", "replace_all": True}
    )
    assert replaced["returncode"] == 0
    assert path.read_text(encoding="utf-8") == "y y"


def test_write_new_file_and_existing_file_guard(tmp_path):
    _, tools = tools_for(tmp_path)
    created = tools["Write"].handler({"file_path": "new.txt", "content": "fresh"})
    assert created["returncode"] == 0
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "fresh"

    # The file was written by this session, so replacing it is allowed.
    replaced = tools["Write"].handler({"file_path": "new.txt", "content": "updated"})
    assert replaced["returncode"] == 0

    other = tmp_path / "other.txt"
    other.write_text("not read", encoding="utf-8")
    refused = tools["Write"].handler({"file_path": "other.txt", "content": "nope"})
    assert refused["returncode"] == -1
    assert "has not been read" in refused["exception_info"]
