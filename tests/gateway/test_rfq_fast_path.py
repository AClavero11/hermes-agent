import json

from gateway.rfq_fast_path import (
    READ_ONLY_RFQ_TOOLS,
    build_rfq_fast_path_response,
    parse_rfq_prompt,
)


def test_parse_turkish_rfq_prompt():
    parsed = parse_rfq_prompt("Turkish pn 767870 qty 1 SV condition")

    assert parsed is not None
    assert parsed.customer == "Turkish"
    assert parsed.part_number == "767870"
    assert parsed.qty == "1"
    assert parsed.condition == "SV"
    assert parsed.sufficient_for_lookup is True


def test_rfq_fast_path_allows_read_only_tools_without_explicit_execution_words():
    calls = []

    def caller(name, args):
        calls.append((name, args))
        if name == "mcp_v11_v11_customer_lookup":
            return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
        if name == "mcp_v11_v11_get_inventory":
            return json.dumps({
                "result": json.dumps({
                    "found": True,
                    "total_qty": 2,
                    "lines": [{"quantity": 1, "condition": "SV"}],
                })
            })
        if name == "mcp_v11_v11_search_sales":
            return json.dumps({"result": json.dumps({"orders": []})})
        raise AssertionError(name)

    result = build_rfq_fast_path_response(
        "Turkish pn 767870 qty 1 SV condition",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=caller,
    )

    assert result is not None
    assert result.parsed.sufficient_for_lookup is True
    assert result.tool_calls == (
        "mcp_v11_v11_customer_lookup",
        "mcp_v11_v11_get_inventory",
        "mcp_v11_v11_search_sales",
    )
    assert "RFQ fast path" in result.response
    assert "No actions executed." not in result.response
    assert "say run, execute, check, update, fix, or do this" not in result.response
    assert not any("send" in name or "write" in name or "atlas" in name.lower() for name, _ in calls)


def test_rfq_fast_path_missing_tools_does_not_enter_planning():
    result = build_rfq_fast_path_response(
        "quote Turkish 767870 qty 1 sv",
        available_tools=set(),
        tool_caller=lambda _name, _args: (_ for _ in ()).throw(AssertionError("no tool calls expected")),
    )

    assert result is not None
    assert result.tool_calls == ()
    assert "Lookup tools unavailable:" in result.response
    assert "No lookup tools executed." in result.response
    assert "Prioritized tasks:" not in result.response

