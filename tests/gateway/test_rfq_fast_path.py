import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.rfq_fast_path import (
    READ_ONLY_RFQ_TOOLS,
    RFQFastPathResult,
    ParsedRFQ,
    build_rfq_followup_without_context_response,
    build_rfq_fast_path_response,
    merge_rfq_followup_prompt,
    parse_contextual_rfq_prompt,
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


def test_quote_comment_without_rfq_payload_does_not_trigger_fast_path():
    assert parse_rfq_prompt("this is a terrible quote") is None
    assert build_rfq_fast_path_response("this is a terrible quote") is None


def test_rfq_followup_merges_missing_qty():
    merged = merge_rfq_followup_prompt(
        "quote aero accessories 5909891 AR condition",
        "qty 1",
    )

    assert merged == "quote aero accessories pn 5909891 qty 1 AR condition"
    parsed = parse_rfq_prompt(merged)
    assert parsed is not None
    assert parsed.customer == "aero accessories"
    assert parsed.part_number == "5909891"
    assert parsed.qty == "1"
    assert parsed.condition == "AR"
    assert parsed.sufficient_for_lookup is True


def test_rfq_field_followup_without_context_is_concise():
    response = build_rfq_followup_without_context_response("qty 1")

    assert "Received: qty 1." in response
    assert "Prioritized tasks" not in response
    assert "Execution blocked" not in response
    assert "No actions executed" not in response


@pytest.mark.asyncio
async def test_gateway_rfq_followup_uses_pending_prompt(monkeypatch):
    from gateway import run as gateway_run
    from gateway.run import GatewayRunner

    calls = []

    async def fake_rfq_result(message):
        calls.append(message)
        if message == "quote aero accessories 5909891 AR condition":
            return RFQFastPathResult(
                response="RFQ fast path\n\nMissing fields: qty",
                parsed=ParsedRFQ(
                    customer="aero accessories",
                    part_number="5909891",
                    qty="",
                    condition="AR",
                    target_price="",
                    free_text=message,
                ),
                tool_calls=(),
                elapsed_seconds=0.0,
            )
        if message == "quote aero accessories pn 5909891 qty 1 AR condition":
            return RFQFastPathResult(
                response="RFQ fast path\n\nParsed RFQ:\n- Qty: 1",
                parsed=ParsedRFQ(
                    customer="aero accessories",
                    part_number="5909891",
                    qty="1",
                    condition="AR",
                    target_price="",
                    free_text=message,
                ),
                tool_calls=(),
                elapsed_seconds=0.0,
            )
        raise AssertionError(message)

    monkeypatch.setattr(gateway_run, "_run_rfq_fast_path_result", fake_rfq_result)
    runner = object.__new__(GatewayRunner)
    runner._pending_rfq_prompts = {}

    first = await GatewayRunner._run_session_rfq_fast_path(
        runner,
        SimpleNamespace(text="quote aero accessories 5909891 AR condition"),
        session_key="telegram:ac",
    )
    second = await GatewayRunner._run_session_rfq_fast_path(
        runner,
        SimpleNamespace(text="qty 1"),
        session_key="telegram:ac",
    )

    assert "Missing fields: qty" in first
    assert "RFQ fast path" in second
    assert "Prioritized tasks" not in second
    assert "Execution blocked" not in second
    assert calls == [
        "quote aero accessories 5909891 AR condition",
        "quote aero accessories pn 5909891 qty 1 AR condition",
    ]
    assert runner._pending_rfq_prompts == {}


@pytest.mark.asyncio
async def test_gateway_does_not_treat_quote_card_reply_as_new_rfq():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._pending_rfq_prompts = {}

    response = await GatewayRunner._run_session_rfq_fast_path(
        runner,
        SimpleNamespace(
            text="this is a terrible quote",
            reply_to_text=(
                "New ILS Quote Ready\n\n"
                "Quote: SO/016873\n"
                "Parts:\n"
                "766956G1 SV — $1,101 (HISTORICAL)\n\n"
                "Approve to generate email draft."
            ),
            reply_to_message_id="123",
        ),
        session_key="telegram:ac",
    )

    assert response == ""
    assert runner._pending_rfq_prompts == {}


@pytest.mark.asyncio
async def test_gateway_field_only_without_pending_rfq_goes_to_main_agent():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._pending_rfq_prompts = {}

    response = await GatewayRunner._run_session_rfq_fast_path(
        runner,
        SimpleNamespace(text="qty 1", reply_to_text="", reply_to_message_id=None),
        session_key="telegram:ac",
    )

    assert response == ""


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


def test_rfq_superoptimizer_extracts_due_date_and_alexandria_sources():
    calls = []

    def caller(name, args):
        calls.append((name, args))
        if name == "mcp_v11_v11_customer_lookup":
            return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
        if name == "mcp_v11_v11_get_inventory":
            return json.dumps({
                "result": json.dumps({
                    "found": True,
                    "total_qty": 3,
                    "lines": [{"quantity": 1, "condition": "SV"}],
                })
            })
        if name == "mcp_v11_v11_search_sales":
            return json.dumps({
                "result": json.dumps({
                    "orders": [
                        {
                            "order": "SO/016849",
                            "date": date.today().isoformat(),
                            "customer": "MEL AVIATION COMPONENTS LTD",
                            "matching_lines": [{"unit_price": 30000}],
                        }
                    ]
                })
            })
        if name == "mcp_alexandria_unified_search":
            assert "Turkish" in args["query"]
            assert "767870" in args["query"]
            return json.dumps({
                "results": [
                    {
                        "title": "Turkish pricing note",
                        "source": "alexandria/advanced/pricing/turkish.md",
                        "snippet": "Prior Turkish IDG pricing context.",
                    }
                ]
            })
        raise AssertionError(name)

    result = build_rfq_fast_path_response(
        "Turkish pn 767870 qty 1 SV condition due 2026-06-15",
        available_tools={*READ_ONLY_RFQ_TOOLS.values(), "mcp_alexandria_unified_search"},
        tool_caller=caller,
    )

    assert result is not None
    assert result.parsed.due_date == "2026-06-15"
    assert result.tool_calls == (
        "mcp_v11_v11_customer_lookup",
        "mcp_v11_v11_get_inventory",
        "mcp_v11_v11_search_sales",
        "mcp_alexandria_unified_search",
    )
    assert "- Due date: 2026-06-15" in result.response
    assert "Alexandria/QMD context:" in result.response
    assert "Turkish pricing note" in result.response
    assert "Source list:" in result.response
    assert "mcp_alexandria_unified_search" in result.response
    assert "Approval required before customer send, V11 write, or Atlas write." in result.response
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


def test_pricing_engine_not_equal_last_sale():
    def caller(name, args):
        if name == "mcp_v11_v11_customer_lookup":
            return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
        if name == "mcp_v11_v11_get_inventory":
            return json.dumps({
                "result": json.dumps({
                    "found": True,
                    "total_qty": 23,
                    "lines": [{"quantity": 8, "condition": "SV"}],
                })
            })
        if name == "mcp_v11_v11_search_sales":
            return json.dumps({
                "result": json.dumps({
                    "orders": [
                        {
                            "order": "SO/016849",
                            "date": date.today().isoformat(),
                            "customer": "MEL AVIATION COMPONENTS LTD",
                            "matching_lines": [{"unit_price": 30000}],
                        }
                    ]
                })
            })
        raise AssertionError(name)

    result = build_rfq_fast_path_response(
        "Turkish pn 767870 qty 1 SV condition",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=caller,
    )

    assert result is not None
    assert "Recommended price:" in result.response
    assert "- $32,500" in result.response
    assert "Confidence:" in result.response
    assert "- high" in result.response
    assert "Reason:" in result.response
    assert "last sale 30000" in result.response
    assert "single unit order supports higher margin" in result.response
    assert "Recommended price:\n- $30,000" not in result.response
    draft = result.response.split("Customer-ready draft:", 1)[1].split("Approval required", 1)[0]
    assert "AC approval" not in draft


def test_contextual_rfq_exact_prompt_triggers_sales_lookup_and_continues():
    calls = []

    def caller(name, args):
        calls.append((name, args))
        if name == "mcp_v11_v11_search_sales" and args.get("customer") == "turkish":
            return json.dumps({
                "result": json.dumps({
                    "orders": [
                        {
                            "order": "SO/IDG1",
                            "date": date.today().isoformat(),
                            "customer": "TURKISH TECHNIC, INC.",
                            "matching_lines": [
                                {
                                    "product": "767870",
                                    "description": "IDG same config",
                                    "condition": "SV",
                                    "qty": 1,
                                    "unit_price": 30000,
                                }
                            ],
                        }
                    ]
                })
            })
        if name == "mcp_v11_v11_customer_lookup":
            return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
        if name == "mcp_v11_v11_get_inventory":
            return json.dumps({
                "result": json.dumps({
                    "found": True,
                    "total_qty": 23,
                    "lines": [{"quantity": 8, "condition": "SV"}],
                })
            })
        if name == "mcp_v11_v11_search_sales" and args.get("part_number") == "767870":
            return json.dumps({
                "result": json.dumps({
                    "orders": [
                        {
                            "order": "SO/IDG1",
                            "date": date.today().isoformat(),
                            "customer": "TURKISH TECHNIC, INC.",
                            "matching_lines": [{"product": "767870", "qty": 1, "unit_price": 30000}],
                        }
                    ]
                })
            })
        raise AssertionError((name, args))

    context = parse_contextual_rfq_prompt("turkish wants that idg we sold before same config sv 1")
    result = build_rfq_fast_path_response(
        "turkish wants that idg we sold before same config sv 1",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=caller,
    )

    assert context is not None
    assert context.customer == "turkish"
    assert context.category == "idg"
    assert context.qty == "1"
    assert context.condition == "SV"
    assert result is not None
    assert result.parsed.part_number == "767870"
    assert result.tool_calls[0] == "mcp_v11_v11_search_sales"
    assert calls[0] == ("mcp_v11_v11_search_sales", {"customer": "turkish", "limit": 12})
    assert "Context resolution:" in result.response
    assert "Part number: 767870" in result.response
    assert "Recommended price:" in result.response
    assert "Prioritized tasks" not in result.response
    assert "Execution blocked" not in result.response
    assert "No actions executed" not in result.response
    assert not any("send" in name or "write" in name or "atlas" in name.lower() for name, _ in calls)


def test_contextual_rfq_ambiguity_returns_top_two():
    def caller(name, args):
        assert name == "mcp_v11_v11_search_sales"
        assert args == {"customer": "turkish", "limit": 12}
        return json.dumps({
            "result": json.dumps({
                "orders": [
                    {
                        "order": "SO/1",
                        "date": date.today().isoformat(),
                        "customer": "TURKISH TECHNIC, INC.",
                        "matching_lines": [
                            {"product": "111111", "description": "IDG same config", "condition": "SV", "qty": 1, "unit_price": 10000}
                        ],
                    },
                    {
                        "order": "SO/2",
                        "date": date.today().isoformat(),
                        "customer": "TURKISH TECHNIC, INC.",
                        "matching_lines": [
                            {"product": "222222", "description": "IDG same config", "condition": "SV", "qty": 1, "unit_price": 12000}
                        ],
                    },
                ]
            })
        })

    result = build_rfq_fast_path_response(
        "turkish wants that idg again sv 1",
        available_tools={READ_ONLY_RFQ_TOOLS["pricing"]},
        tool_caller=caller,
    )

    assert result is not None
    assert result.tool_calls == ("mcp_v11_v11_search_sales",)
    assert "Multiple plausible prior-sale matches:" in result.response
    assert "111111" in result.response
    assert "222222" in result.response
    assert "Prioritized tasks" not in result.response
    assert "Execution blocked" not in result.response
    assert "No actions executed" not in result.response


def test_contextual_rfq_no_match_asks_for_pn():
    def caller(name, args):
        assert name == "mcp_v11_v11_search_sales"
        return json.dumps({
            "result": json.dumps({
                "orders": [
                    {
                        "order": "SO/OTHER",
                        "date": date.today().isoformat(),
                        "customer": "TURKISH TECHNIC, INC.",
                        "matching_lines": [
                            {"product": "999999", "description": "pump", "condition": "SV", "qty": 1, "unit_price": 1000}
                        ],
                    }
                ]
            })
        })

    result = build_rfq_fast_path_response(
        "turkish wants that idg previous sv 1",
        available_tools={READ_ONLY_RFQ_TOOLS["pricing"]},
        tool_caller=caller,
    )

    assert result is not None
    assert result.tool_calls == ("mcp_v11_v11_search_sales",)
    assert "Prior sales match: none strong enough to identify a PN." in result.response
    assert "send the PN" in result.response
    assert "Prioritized tasks" not in result.response
    assert "Execution blocked" not in result.response
    assert "No actions executed" not in result.response


def test_contextual_rfq_uses_pricing_export_when_sales_tool_has_no_lines(tmp_path, monkeypatch):
    pricing = tmp_path / "alexandria" / "advanced" / "pricing"
    data_dir = pricing / "data"
    data_dir.mkdir(parents=True)
    (pricing / "IDG_PIECE_PARTS_HOT_LIST.md").write_text(
        "| PN | Description |\n| 767886 | Gearshaft |\n| 761870 | Fixed Shaft |\n",
        encoding="utf-8",
    )
    (pricing / "oem_master_2026.csv").write_text("PN,DESCRIPTION\n767886,GEARSHFT\n", encoding="utf-8")
    (data_dir / "v11_completed_sales.csv").write_text(
        "\n".join([
            "order_number,date_order,order_state,customer_name,country_id,part_number,default_code,quantity,unit_price,line_total,discount,order_total",
            "SO/OLD,2025-11-17 00:49:06,sale,\"TURKISH TECHNIC, INC.\",224,761870,,1,8500.00,8500.00,0.00,8500.00",
            "SO/NEW,2025-12-23 16:33:55,sale,\"TURKISH TECHNIC, INC.\",224,767886,,1,7300.00,7300.00,0.00,7300.00",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def caller(name, args):
        calls.append((name, args))
        if name == "mcp_v11_v11_search_sales" and args.get("customer") == "turkish":
            return json.dumps({
                "result": json.dumps({
                    "orders": [
                        {
                            "order": "SO/NEW",
                            "date": "2025-12-23 16:33:55",
                            "customer": "TURKISH TECHNIC, INC.",
                            "matching_lines": [],
                        }
                    ]
                })
            })
        if name == "mcp_v11_v11_customer_lookup":
            return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
        if name == "mcp_v11_v11_get_inventory":
            return json.dumps({
                "result": json.dumps({
                    "found": True,
                    "total_qty": 5,
                    "lines": [{"quantity": 2, "condition": "SV"}],
                })
            })
        if name == "mcp_v11_v11_search_sales" and args.get("part_number") == "767886":
            return json.dumps({"result": json.dumps({"orders": []})})
        raise AssertionError((name, args))

    result = build_rfq_fast_path_response(
        "turkish wants that idg we sold before same config sv 1",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=caller,
    )

    assert result is not None
    assert result.parsed.part_number == "767886"
    assert "Context resolution:" in result.response
    assert "Recommended price:" in result.response
    assert "manual pricing required" not in result.response
    assert "Context prior sale" in result.response
    assert calls[0] == ("mcp_v11_v11_search_sales", {"customer": "turkish", "limit": 12})


def test_contextual_rfq_infers_missing_fields():
    calls = []

    def caller(name, args):
        calls.append((name, args))
        if name == "mcp_v11_v11_search_sales" and args.get("customer") == "turkish":
            return json.dumps({
                "result": json.dumps({
                    "orders": [
                        {
                            "order": "SO/IDG1",
                            "date": date.today().isoformat(),
                            "customer": "TURKISH TECHNIC, INC.",
                            "matching_lines": [
                                {
                                    "product": "767870",
                                    "description": "IDG same config",
                                    "condition": "SV",
                                    "qty": 2,
                                    "unit_price": 30000,
                                }
                            ],
                        }
                    ]
                })
            })
        if name == "mcp_v11_v11_customer_lookup":
            return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
        if name == "mcp_v11_v11_get_inventory":
            return json.dumps({
                "result": json.dumps({
                    "found": True,
                    "total_qty": 23,
                    "lines": [{"quantity": 8, "condition": "SV"}],
                })
            })
        if name == "mcp_v11_v11_search_sales" and args.get("part_number") == "767870":
            return json.dumps({"result": json.dumps({"orders": []})})
        raise AssertionError((name, args))

    result = build_rfq_fast_path_response(
        "turkish wants that same idg again",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=caller,
    )

    assert result is not None
    assert result.parsed.part_number == "767870"
    assert result.parsed.qty == "1"
    assert result.parsed.condition == "SV"
    assert "Missing fields:" not in result.response
    assert "Inferred fields:" in result.response
    assert "- part_number: 767870 (from prior sale)" in result.response
    assert "- qty: 1 (default)" in result.response
    assert "- condition: SV (from last sale)" in result.response
    assert "Prioritized tasks" not in result.response
    assert "Execution blocked" not in result.response
    assert "No actions executed" not in result.response
    assert not any("send" in name or "write" in name or "atlas" in name.lower() for name, _ in calls)


def test_pricing_modifier_push_price():
    def make_caller():
        def caller(name, args):
            if name == "mcp_v11_v11_search_sales" and args.get("customer") == "turkish":
                return json.dumps({
                    "result": json.dumps({
                        "orders": [
                            {
                                "order": "SO/IDG1",
                                "date": date.today().isoformat(),
                                "customer": "TURKISH TECHNIC, INC.",
                                "matching_lines": [
                                    {
                                        "product": "767870",
                                        "description": "IDG same config",
                                        "condition": "SV",
                                        "qty": 1,
                                        "unit_price": 30000,
                                    }
                                ],
                            }
                        ]
                    })
                })
            if name == "mcp_v11_v11_customer_lookup":
                return json.dumps({"result": json.dumps({"customers": [{"name": "TURKISH TECHNIC, INC."}]})})
            if name == "mcp_v11_v11_get_inventory":
                return json.dumps({
                    "result": json.dumps({
                        "found": True,
                        "total_qty": 23,
                        "lines": [{"quantity": 8, "condition": "SV"}],
                    })
                })
            if name == "mcp_v11_v11_search_sales" and args.get("part_number") == "767870":
                return json.dumps({"result": json.dumps({"orders": []})})
            raise AssertionError((name, args))
        return caller

    baseline = build_rfq_fast_path_response(
        "turkish wants that same idg again",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=make_caller(),
    )
    pushed = build_rfq_fast_path_response(
        "turkish wants that same idg again but push price",
        available_tools=set(READ_ONLY_RFQ_TOOLS.values()),
        tool_caller=make_caller(),
    )

    assert baseline is not None
    assert pushed is not None
    assert "Recommended price:\n- $32,500" in baseline.response
    assert "Recommended price:\n- $33,000" in pushed.response
    assert "price increased due to push price request" in pushed.response
    assert "Inferred fields:" in pushed.response
    assert "- qty: 1 (default)" in pushed.response
    assert "- condition: SV (from last sale)" in pushed.response
    assert "Missing fields:" not in pushed.response
