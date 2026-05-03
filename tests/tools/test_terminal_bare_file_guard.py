import json

from tools.terminal_tool import terminal_tool


def test_terminal_rejects_bare_markdown_filename():
    result = json.loads(terminal_tool("notification_rules.md"))

    assert result["status"] == "error"
    assert result["exit_code"] == -1
    assert "looks like a file path" in result["error"]
    assert "file/read-file tool" in result["error"]


def test_terminal_rejects_bare_relative_and_absolute_files():
    for command in ("./notification_rules.md", "/tmp/notification_rules.md"):
        result = json.loads(terminal_tool(command))
        assert result["status"] == "error"
        assert "looks like a file path" in result["error"]

