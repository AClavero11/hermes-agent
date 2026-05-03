from __future__ import annotations

import json
from pathlib import Path

import hermes_cli.canary as canary_module
from hermes_cli.canary import (
    PASS,
    SKIP,
    WARN,
    CanaryReport,
    CanaryOptions,
    CanaryResult,
    quality_summary,
    readiness_summary,
    render_markdown,
    report_to_dict,
    run_canary_suite,
    write_report,
)


def test_canary_suite_runs_without_live_gateway(tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="http://127.0.0.1:1",
        env_wrapper=None,
        require_live=False,
        timeout=0.2,
    )

    report = run_canary_suite(options)
    names = {result.name for result in report.results}

    assert "runtime.imports" in names
    assert "contract.workspace_store" in names
    assert "contract.goal_workspace" in names
    assert any(
        result.name == "contract.aac_workflows" and result.status == PASS
        for result in report.results
    )
    assert any(
        result.name == "live.gateway_health" and result.status == SKIP
        for result in report.results
    )
    assert any(
        result.name == "live.telegram_e2e" and result.status == WARN
        for result in report.results
    )
    assert any(
        result.name == "live.telegram_visible_delivery" and result.status == SKIP
        for result in report.results
    )
    assert any(
        result.name == "eval.local_model_reasoning" and result.status == SKIP
        for result in report.results
    )
    assert any(
        result.name == "eval.hermes_reasoning" and result.status == SKIP
        for result in report.results
    )
    assert any(
        result.name == "eval.frontier_wrapper" and result.status == SKIP
        for result in report.results
    )
    assert any(
        result.name == "live.rfq_dry_run_quote_package" and result.status == SKIP
        for result in report.results
    )
    assert any(
        result.name == "live.approved_rfq_draft_quote" and result.status == SKIP
        for result in report.results
    )
    assert report.effective_max_score > 0


def test_options_from_args_infers_home_from_env_wrapper(tmp_path):
    wrapper_home = tmp_path / ".hermes-deepseek"
    wrapper = wrapper_home / "bin" / "hermes-env.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# test wrapper\n", encoding="utf-8")

    parser = canary_module.build_arg_parser()
    args = parser.parse_args(["--env-wrapper", str(wrapper)])
    options = canary_module.options_from_args(args)

    assert options.hermes_home == wrapper_home
    assert options.env_wrapper == wrapper


def test_options_from_args_respects_explicit_hermes_home(tmp_path):
    explicit_home = tmp_path / "explicit-home"
    wrapper_home = tmp_path / ".hermes-deepseek"
    wrapper = wrapper_home / "bin" / "hermes-env.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# test wrapper\n", encoding="utf-8")

    parser = canary_module.build_arg_parser()
    args = parser.parse_args([
        "--hermes-home",
        str(explicit_home),
        "--env-wrapper",
        str(wrapper),
    ])
    options = canary_module.options_from_args(args)

    assert options.hermes_home == explicit_home
    assert options.env_wrapper == wrapper


def test_live_behavior_allows_loopback_without_api_key(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="http://127.0.0.1:8643",
        live_behavior=True,
        timeout=0.2,
    )

    def fake_run_responses_case(*, url, api_key, case, timeout):
        return {"ok": True, "text": "ok", "elapsed_ms": 1.0}

    monkeypatch.setattr(canary_module, "_run_responses_case", fake_run_responses_case)
    monkeypatch.setattr(canary_module, "_resolve_api_key", lambda options: ("", ""))

    result = canary_module._canary_live_behavior(options)

    assert result.status == PASS
    assert result.details["api_key_present"] is False
    assert result.details["api_key_source"] == "loopback_unauthenticated"
    assert result.details["loopback_unauthenticated"] is True


def test_live_behavior_still_requires_api_key_for_remote_gateway(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="https://hermes.advanced.aero",
        live_behavior=True,
        timeout=0.2,
    )
    monkeypatch.setattr(canary_module, "_resolve_api_key", lambda options: ("", ""))

    result = canary_module._canary_live_behavior(options)

    assert result.status == SKIP
    assert result.summary == "No API key available for /v1/responses"


def test_hermes_reasoning_allows_loopback_without_api_key(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="http://localhost:8643",
        reasoning_eval=True,
        timeout=0.2,
    )

    def fake_run_responses_case(*, url, api_key, case, timeout):
        return {"ok": True, "text": "ok", "elapsed_ms": 1.0}

    monkeypatch.setattr(canary_module, "_run_responses_case", fake_run_responses_case)
    monkeypatch.setattr(canary_module, "_resolve_api_key", lambda options: ("", ""))

    result = canary_module._canary_hermes_reasoning_eval(options)

    assert result.status == PASS
    assert result.details["api_key_present"] is False
    assert result.details["api_key_source"] == "loopback_unauthenticated"
    assert result.details["loopback_unauthenticated"] is True


def test_hermes_reasoning_still_requires_api_key_for_remote_gateway(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="https://hermes.advanced.aero",
        reasoning_eval=True,
        timeout=0.2,
    )
    monkeypatch.setattr(canary_module, "_resolve_api_key", lambda options: ("", ""))

    result = canary_module._canary_hermes_reasoning_eval(options)

    assert result.status == SKIP
    assert result.summary == "No API key available for /v1/responses"


def test_canary_writes_markdown_and_json(tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        require_live=False,
        timeout=0.2,
    )
    report = run_canary_suite(options)

    write_report(report, tmp_path / "reports")

    markdown_path = Path(report.markdown_path)
    json_path = Path(report.json_path)
    assert markdown_path.is_file()
    assert json_path.is_file()
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Hermes Capability Metrics" in markdown
    assert "Frontier readiness" in markdown
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["results"]
    assert payload["readiness"]["status"] == "not_frontier_ready"
    assert payload["markdown_path"] == str(markdown_path)
    assert (tmp_path / "reports" / "latest.json").is_file()
    assert (tmp_path / "reports" / "latest.md").is_file()
    history_path = tmp_path / "reports" / "history.jsonl"
    assert history_path.is_file()
    assert json.loads(history_path.read_text(encoding="utf-8").splitlines()[-1])


def test_report_markdown_contains_metrics_tables(tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        require_live=False,
        timeout=0.2,
    )
    report = run_canary_suite(options)
    markdown = render_markdown(report)
    payload = report_to_dict(report)

    assert "| Gate | Status | Requirement | Evidence |" in markdown
    assert "| Canary | Status | Score | Summary |" in markdown
    assert "local_model_reasoning" in markdown
    assert "hermes_reasoning_eval" in markdown
    assert "contract.x_scrape" in markdown
    assert payload["effective_max_score"] == report.effective_max_score
    assert payload["readiness"]["total"] >= 8
    assert any(result.status == PASS for result in report.results)


def test_score_text_case_accepts_final_numeric_answer():
    result = canary_module._score_text_case(
        status=200,
        text=(
            "First, compute 3 * 6 * 4 = 72.\n"
            "Then compute 1 * 6 * 0.5 = 3.\n"
            "FINAL: 69"
        ),
        raw="",
        elapsed_ms=100.0,
        case={
            "expected_final_number": "69",
            "forbidden": ["sorry", "cannot", "as an ai"],
            "latency_budget_ms": 1000,
        },
    )

    assert result["ok"] is True
    assert result["final_number"] == "69"


def test_local_reasoning_cases_use_structured_arithmetic():
    cases = canary_module._reasoning_eval_cases(
        latency_budget_ms=45_000,
        scaffold_arithmetic=True,
    )
    arithmetic_cases = [case for case in cases if case["name"].startswith("multi_step_arithmetic")]

    assert len(arithmetic_cases) == 3
    assert {case["expected_final_number"] for case in arithmetic_cases} == {
        "69",
        "5650",
        "39000",
    }
    assert all('Return JSON only: {"answer": integer}' in case["input"] for case in arithmetic_cases)
    assert all(case["max_tokens"] == 32 for case in arithmetic_cases)
    capacity_case = next(case for case in arithmetic_cases if case["name"] == "multi_step_arithmetic_capacity")
    assert "lost capacity" in capacity_case["input"]
    assert "subtract lost capacity from total capacity" in capacity_case["input"]


def _quality_foundation_results() -> list[CanaryResult]:
    return [
        CanaryResult("contract.behavior_goldens", PASS, 20, 20, "behavior goldens passed"),
        CanaryResult("live.behavior_golden", PASS, 20, 20, "live behavior passed"),
        CanaryResult("contract.scorecard_trend", PASS, 10, 10, "trend installed"),
        CanaryResult("contract.aac_workflows", PASS, 15, 15, "AAC workflows passed"),
        CanaryResult("contract.planner_self_heal", PASS, 20, 20, "planner self-heal passed"),
        CanaryResult(
            "runtime.model_route",
            PASS,
            15,
            15,
            "custom:office-deepseek-v4 -> mlx-community/deepseek-ai-DeepSeek-V4-Flash-4bit",
        ),
        CanaryResult("eval.hermes_reasoning", PASS, 30, 30, "full Hermes reasoning passed"),
        CanaryResult("eval.frontier_wrapper", PASS, 20, 20, "frontier wrapper passed"),
        CanaryResult("live.telegram_e2e", PASS, 20, 20, "Telegram E2E passed"),
    ]


def test_quality_score_floor_tracks_completed_increment():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            *_quality_foundation_results(),
            CanaryResult(
                "eval.local_model_reasoning",
                PASS,
                30,
                30,
                "direct DeepSeek reasoning passed",
            ),
        ],
    )

    summary = quality_summary(report)

    assert summary["score"] >= 9.0
    assert [item["status"] for item in summary["increments"][:5]] == [
        "done",
        "done",
        "done",
        "done",
        "done",
    ]


def test_quality_score_9_requires_telegram_e2e():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            CanaryResult(
                "contract.behavior_goldens",
                PASS,
                20,
                20,
                "behavior goldens passed",
            ),
            CanaryResult(
                "live.behavior_golden",
                PASS,
                20,
                20,
                "live behavior passed",
            ),
            CanaryResult(
                "contract.scorecard_trend",
                PASS,
                10,
                10,
                "trend installed",
            ),
            CanaryResult(
                "contract.aac_workflows",
                PASS,
                15,
                15,
                "AAC workflows passed",
            ),
            CanaryResult(
                "contract.planner_self_heal",
                PASS,
                20,
                20,
                "planner self-heal passed",
            ),
            CanaryResult(
                "live.telegram_e2e",
                WARN,
                0,
                20,
                "No Telegram E2E evidence",
            ),
        ],
    )

    summary = quality_summary(report)
    readiness = readiness_summary(report)

    assert summary["score"] == 8.5
    assert summary["increments"][4]["target"] == "9.0/10"
    assert summary["increments"][4]["status"] == "open"
    assert readiness["status"] == "not_frontier_ready"
    assert {gate["name"] for gate in readiness["open_gates"]} >= {
        "local_deepseek_route",
        "hermes_reasoning_eval",
        "frontier_wrapper",
        "telegram_e2e",
    }
    assert readiness["diagnostics"][0]["name"] == "raw_local_model_reasoning"


def test_quality_score_9_5_requires_live_rfq_dry_run():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            *_quality_foundation_results(),
            CanaryResult("contract.quote_ops_runtime", PASS, 20, 20, "quote ops runtime passed"),
            CanaryResult(
                "live.rfq_dry_run_quote_package",
                PASS,
                25,
                25,
                "live RFQ dry-run package passed",
            ),
        ],
    )

    summary = quality_summary(report)

    increment = next(item for item in summary["increments"] if item["target"] == "9.5/10")
    assert increment["status"] == "done"
    assert summary["score"] >= 9.5
    business_ops = next(item for item in summary["dimensions"] if item["name"] == "business_ops")
    assert business_ops["score"] == 9.5


def test_quality_score_9_7_requires_approved_rfq_draft():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            *_quality_foundation_results(),
            CanaryResult("contract.quote_ops_runtime", PASS, 20, 20, "quote ops runtime passed"),
            CanaryResult(
                "live.rfq_dry_run_quote_package",
                PASS,
                25,
                25,
                "live RFQ dry-run package passed",
            ),
            CanaryResult(
                "live.approved_rfq_draft_quote",
                PASS,
                30,
                30,
                "approved RFQ draft package passed",
            ),
        ],
    )

    summary = quality_summary(report)

    increment = next(item for item in summary["increments"] if item["target"] == "9.7/10")
    assert increment["status"] == "done"
    assert summary["score"] >= 9.7
    business_ops = next(item for item in summary["dimensions"] if item["name"] == "business_ops")
    assert business_ops["score"] == 9.7


def test_quality_score_does_not_skip_foundation_for_business_gates():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            CanaryResult("contract.quote_ops_runtime", PASS, 20, 20, "quote ops runtime passed"),
            CanaryResult(
                "live.rfq_dry_run_quote_package",
                PASS,
                25,
                25,
                "live RFQ dry-run package passed",
            ),
            CanaryResult(
                "live.approved_rfq_draft_quote",
                PASS,
                30,
                30,
                "approved RFQ draft package passed",
            ),
        ],
    )

    summary = quality_summary(report)

    assert next(item for item in summary["increments"] if item["target"] == "9.0/10")["status"] == "open"
    assert next(item for item in summary["increments"] if item["target"] == "9.7/10")["status"] == "open"
    assert summary["score"] < 9.0


def test_frontier_wrapper_falls_back_to_gemini(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        frontier_eval=True,
        require_live=True,
        timeout=0.2,
    )
    monkeypatch.setenv("HERMES_FRONTIER_PROVIDER", "auto")
    monkeypatch.setattr(
        canary_module,
        "_run_openai_frontier_probe",
        lambda _options: {"provider": "openai", "ok": False, "summary": "OpenAI quota blocked"},
    )
    monkeypatch.setattr(
        canary_module,
        "_run_gemini_frontier_probe",
        lambda _options: {
            "provider": "gemini",
            "ok": True,
            "summary": "gemini-2.5-pro Gemini wrapper passed structured reasoning probe in 1000ms",
            "model": "gemini-2.5-pro",
            "latency_ms": 1000.0,
            "usage": {},
            "parsed": {"ok": True, "answer": 59, "contract": "schema-followed"},
        },
    )

    result = canary_module._canary_frontier_wrapper(options)

    assert result.status == PASS
    assert result.details["provider"] == "gemini"
    assert [attempt["provider"] for attempt in result.details["attempts"]] == ["openai", "gemini"]


def test_telegram_e2e_accepts_signed_webhook_simulation_evidence(tmp_path):
    evidence_path = tmp_path / "canary" / "telegram_e2e_last.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        json.dumps(
            {
                "status": "pass",
                "mode": "signed_webhook_simulation",
                "source": "telegram_webhook_update",
                "latency_ms": 250,
                "latency_budget_ms": 15000,
                "no_interruption": True,
                "no_capability_refusal": True,
                "restart_during_task": False,
                "nonce": "sim-test",
            }
        ),
        encoding="utf-8",
    )
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        require_live=True,
        timeout=0.2,
    )

    result = canary_module._canary_telegram_e2e(options)

    assert result.status == PASS
    assert result.details["payload"]["mode"] == "signed_webhook_simulation"
