import json

from tools.terminal_tool import terminal_tool


def test_model_route_string_is_not_executed_as_terminal_command():
    result = json.loads(terminal_tool("gemini:gemini-2.5-flash"))

    assert result["status"] == "error"
    assert result["exit_code"] == -1
    assert "model/provider route" in result["error"]
    assert result["output"] == ""

