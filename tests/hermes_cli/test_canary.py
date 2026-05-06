from __future__ import annotations

import json
import os
import time
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
    assert "runtime.model_routes" in names
    assert "contract.workspace_store" in names
    assert "contract.workflow_registry" in names
    assert "contract.goal_workspace" in names
    assert any(
        result.name == "contract.aac_workflows" and result.status == PASS
        for result in report.results
    )
    assert any(
        result.name == "contract.memory_grounding" and result.status in {PASS, WARN}
        for result in report.results
    )
    assert any(
        result.name == "contract.business_os_brief" and result.status == PASS
        for result in report.results
    )
    assert any(
        result.name == "contract.business_os_daily_report" and result.status == PASS
        for result in report.results
    )
    assert any(
        result.name == "contract.aeroxchange_browser_workflow" and result.status == PASS
        for result in report.results
    )
    assert "contract.browser_harness" in names
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
        result.name == "live.telegram_operator_response" and result.status == SKIP
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


def test_browser_harness_canary_passes_with_installed_skill(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "browser-harness"
    executable.write_text("#!/bin/sh\nprintf 'Browser Harness\\n'\n", encoding="utf-8")
    executable.chmod(0o755)

    home = tmp_path / "home"
    repo = home / "tools" / "browser-harness"
    repo.mkdir(parents=True)
    for name in ("SKILL.md", "helpers.py", "install.md"):
        (repo / name).write_text(name, encoding="utf-8")

    hermes_home = tmp_path / "hermes-home"
    skill_dir = hermes_home / "skills" / "browser-harness"
    skill_dir.mkdir(parents=True)
    for name in ("SKILL.md", "helpers.py", "install.md"):
        (skill_dir / name).write_text(name, encoding="utf-8")
    (skill_dir / "interaction-skills").mkdir()
    (skill_dir / "domain-skills").mkdir()

    provider_source = tmp_path / "repo" / "tools" / "browser_providers"
    provider_source.mkdir(parents=True)
    (provider_source / "browser_use.py").write_text("# provider\n", encoding="utf-8")

    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("BROWSER_USE_API_KEY", "test-key")
    monkeypatch.setattr(canary_module.Path, "home", lambda: home)
    options = CanaryOptions(
        repo_root=tmp_path / "repo",
        hermes_home=hermes_home,
        env_wrapper=None,
        timeout=0.2,
    )

    result = canary_module._canary_browser_harness_contract(options)

    assert result.status == PASS
    assert result.details["browser_use_api_key_present"] is True


def test_release_profile_converts_skipped_live_checks_to_failures(tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="http://127.0.0.1:1",
        env_wrapper=None,
        release_profile=True,
        timeout=0.2,
    )

    report = run_canary_suite(options)
    quality = quality_summary(report)

    assert report.runtime["canary_profile"] == "release"
    assert any(
        result.name == "live.behavior_golden" and result.status == canary_module.FAIL
        for result in report.results
    )
    assert any(
        result.name == "live.telegram_visible_delivery" and result.status == canary_module.FAIL
        for result in report.results
    )
    assert quality["score"] <= 8.9


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


def test_options_from_args_release_profile_requires_live(tmp_path):
    parser = canary_module.build_arg_parser()
    args = parser.parse_args(["--release-profile", "--hermes-home", str(tmp_path)])
    options = canary_module.options_from_args(args)

    assert options.release_profile is True
    assert options.require_live is True


def test_current_repo_sha_reads_deploy_runtime_version(tmp_path):
    (tmp_path / ".hermes-runtime-version.json").write_text(
        json.dumps({"sha": "runtime-sha-123"}) + "\n",
        encoding="utf-8",
    )

    assert canary_module._current_repo_sha(tmp_path) == "runtime-sha-123"


def test_model_routes_canary_passes_with_verified_frontier(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_PLANNER_PROVIDER", "custom:office-deepseek-v4")
    monkeypatch.setenv("HERMES_PLANNER_MODEL", "deepseek-v4")
    monkeypatch.setenv("HERMES_OPENAI_FRONTIER_AVAILABLE", "1")
    monkeypatch.setenv("OPENAI_FRONTIER_MODEL", "gpt-5.4-mini")
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        env_wrapper=None,
        timeout=0.2,
    )

    result = canary_module._canary_model_routes(options)

    assert result.status == PASS
    assert "hard_task_planner=custom:openai-frontier:gpt-5.4-mini" in result.summary


def test_model_routes_canary_warns_without_frontier(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_PLANNER_PROVIDER", "custom:office-deepseek-v4")
    monkeypatch.setenv("HERMES_PLANNER_MODEL", "deepseek-v4")
    monkeypatch.delenv("HERMES_OPENAI_FRONTIER_AVAILABLE", raising=False)
    monkeypatch.delenv("HERMES_GEMINI_FRONTIER_AVAILABLE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        env_wrapper=None,
        timeout=0.2,
    )

    result = canary_module._canary_model_routes(options)

    assert result.status == WARN
    assert "no frontier planner route" in result.summary


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


def test_score_text_case_matches_terms_case_insensitively():
    result = canary_module._score_text_case(
        status=200,
        text="RFQ lane. V11 grounded. Approval required before sends.",
        raw="",
        elapsed_ms=100.0,
        case={
            "required": ["rfq", "v11", "approval"],
            "forbidden": ["I'M SORRY", "CANNOT"],
            "latency_budget_ms": 1000,
        },
    )

    assert result["ok"] is True
    assert result["missing"] == []
    assert result["forbidden"] == []


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
        CanaryResult("live.gateway_health", PASS, 15, 15, "gateway health passed"),
        CanaryResult("contract.scorecard_trend", PASS, 10, 10, "trend installed"),
        CanaryResult("contract.aac_workflows", PASS, 15, 15, "AAC workflows passed"),
        CanaryResult("contract.memory_grounding", PASS, 20, 20, "memory grounding passed"),
        CanaryResult("contract.business_os_brief", PASS, 20, 20, "business OS brief passed"),
        CanaryResult("contract.business_os_daily_report", PASS, 20, 20, "business OS daily passed"),
        CanaryResult("contract.x_scrape", PASS, 10, 10, "X scrape contract passed"),
        CanaryResult("contract.workspace_store", PASS, 10, 10, "Workspace store passed"),
        CanaryResult("contract.workflow_registry", PASS, 15, 15, "Workflow registry passed"),
        CanaryResult("contract.goal_workspace", PASS, 15, 15, "Goal workspace passed"),
        CanaryResult("contract.operator_safety", PASS, 10, 10, "Operator safety passed"),
        CanaryResult("contract.planner_self_heal", PASS, 20, 20, "planner self-heal passed"),
        CanaryResult(
            "runtime.model_route",
            PASS,
            15,
            15,
            "custom:office-deepseek-v4 -> mlx-community/deepseek-ai-DeepSeek-V4-Flash-4bit",
        ),
        CanaryResult(
            "runtime.model_routes",
            PASS,
            20,
            20,
            "planner=custom:office-deepseek-v4:deepseek-v4, hard_task_planner=custom:openai-frontier:gpt-5.4-mini",
        ),
        CanaryResult("eval.hermes_reasoning", PASS, 30, 30, "full Hermes reasoning passed"),
        CanaryResult("eval.frontier_wrapper", PASS, 20, 20, "frontier wrapper passed"),
        CanaryResult("live.telegram_e2e", PASS, 20, 20, "Telegram E2E passed"),
        CanaryResult(
            "live.telegram_operator_response",
            PASS,
            20,
            20,
            "Telegram operator response passed",
        ),
        CanaryResult("live.telegram_visible_delivery", PASS, 20, 20, "Visible Telegram delivery passed"),
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
    assert next(item for item in summary["increments"] if item["target"] == "9.0/10")["status"] == "open"
    assert readiness["status"] == "not_frontier_ready"
    assert {gate["name"] for gate in readiness["open_gates"]} >= {
        "local_deepseek_executor",
        "hermes_reasoning_eval",
        "frontier_wrapper",
        "telegram_e2e",
        "telegram_operator_response",
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


def test_quality_score_caps_without_visible_telegram_delivery():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            *[
                result
                for result in _quality_foundation_results()
                if result.name != "live.telegram_visible_delivery"
            ],
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

    assert summary["score"] <= 8.9
    assert summary["caps"][0]["reason"] == "human-visible Telegram delivery is not currently proven"
    assert next(item for item in summary["increments"] if item["target"] == "9.0/10")["status"] == "open"


def test_quality_score_caps_without_telegram_operator_response():
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            *[
                result
                for result in _quality_foundation_results()
                if result.name != "live.telegram_operator_response"
            ],
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

    assert summary["score"] <= 8.9
    assert {
        cap["reason"]
        for cap in summary["caps"]
    } >= {"Telegram operator prompt response is not currently proven"}
    assert next(item for item in summary["increments"] if item["target"] == "9.0/10")["status"] == "open"


def test_telegram_operator_expected_substrings_follow_prompt_choice():
    assert canary_module._telegram_operator_expected_substrings("what can we do") == [
        "RFQ",
        "V11",
        "follow",
        "Hermes",
        "code",
        "approval",
    ]
    assert canary_module._telegram_operator_expected_substrings("ack") == [
        "Ack received",
        "No task started",
    ]
    assert canary_module._telegram_operator_expected_substrings("testing") == [
        "Hermes online",
        "Planner:",
        "Executor:",
        "Judge:",
    ]
    assert canary_module._telegram_operator_expected_substrings("status") == [
        "Hermes Gateway Status",
        "Agent Running",
    ]
    assert canary_module._telegram_operator_expected_substrings("new") == [
        "Session",
        "fresh",
    ]
    assert canary_module._telegram_operator_expected_substrings("stop") == [
        "active task",
    ]
    assert canary_module._telegram_operator_expected_substrings("rfq") == [
        "RFQ mode",
        "draft package",
        "No customer sends",
    ]
    assert canary_module._telegram_operator_expected_substrings("qty 1") == [
        "Received: qty 1",
    ]
    assert canary_module._telegram_operator_forbidden_substrings("qty 1") == [
        "cannot",
        "sorry",
        "waiting for model",
        "Prioritized tasks",
        "Execution blocked",
        "No actions executed",
    ]
    assert canary_module._telegram_operator_expected_substrings("inventory") == [
        "Inventory mode",
        "V11",
        "read-only",
    ]
    assert canary_module._telegram_operator_expected_substrings("followups") == [
        "Follow-up mode",
        "drafts only",
        "approval",
    ]
    assert canary_module._telegram_operator_expected_substrings("hermes") == [
        "Hermes mode",
        "runtime path",
        "canary",
    ]


def test_telegram_operator_response_probe_accepts_expected_menu(monkeypatch, tmp_path):
    current_sha = "abcdef1234567890"
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        telegram_operator_probe=True,
        require_live=True,
        timeout=0.2,
    )

    monkeypatch.setattr(
        canary_module,
        "_run_telegram_operator_response_probe",
        lambda options: {
            "ok": True,
            "nonce": "op-test",
            "prompt": "what can we do",
            "evidence": {
                "status": "pass",
                "mode": "signed_operator_response_probe",
                "latency_ms": 500,
                "latency_budget_ms": 10_000,
                "content_match": True,
                "telegram_send_ok": True,
                "nonce": "op-test",
                "prompt": "what can we do",
                "repo_sha": current_sha,
                "created_at": time.time(),
            },
        },
    )
    monkeypatch.setattr(canary_module, "_current_repo_sha", lambda _repo_root: current_sha)

    result = canary_module._canary_telegram_operator_response(options)

    assert result.status == PASS
    assert result.details["checks"]["content_match"] is True
    assert "500ms" in result.summary


def test_telegram_operator_response_probe_flags_timeout(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        telegram_operator_probe=True,
        require_live=False,
        timeout=0.2,
    )

    monkeypatch.setattr(
        canary_module,
        "_run_telegram_operator_response_probe",
        lambda options: {"ok": False, "error": "missing"},
    )

    result = canary_module._canary_telegram_operator_response(options)

    assert result.status == WARN
    assert result.details["failure_class"] == "telegram_operator_response_missing"


def test_telegram_visible_delivery_rejects_stale_evidence(monkeypatch, tmp_path):
    current_sha = "abcdef1234567890"
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        telegram_visible_probe=True,
        require_live=False,
        timeout=0.2,
    )

    monkeypatch.setattr(canary_module, "_current_repo_sha", lambda _repo_root: current_sha)
    monkeypatch.setattr(
        canary_module,
        "_run_telegram_visible_probe",
        lambda options: {
            "ok": True,
            "nonce": "real-test",
            "evidence": {
                "status": "pass",
                "mode": "real_visible_delivery_probe",
                "latency_ms": 500,
                "latency_budget_ms": 60_000,
                "ack_match": "exact",
                "nonce": "real-test",
                "repo_sha": current_sha,
                "created_at": time.time() - 3600,
            },
        },
    )

    result = canary_module._canary_telegram_visible_delivery(options)
    now = 1_700_000_000.0
    report = CanaryReport(
        started_at=now,
        finished_at=now + 1,
        fail_under=80.0,
        results=[
            *[
                item
                for item in _quality_foundation_results()
                if item.name != "live.telegram_visible_delivery"
            ],
            result,
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
    quality = quality_summary(report)

    assert result.status == WARN
    assert result.details["failure_class"] == "telegram_visible_stale_or_unbound_evidence"
    assert "fresh" in result.details["evidence_validation"]["failed"]
    assert quality["score"] <= 8.9


def test_telegram_operator_prompt_specific_fresh_evidence_passes(monkeypatch, tmp_path):
    current_sha = "abcdef1234567890"
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        telegram_operator_probe=True,
        require_live=True,
        timeout=0.2,
    )
    monkeypatch.setattr(canary_module, "_current_repo_sha", lambda _repo_root: current_sha)
    monkeypatch.setattr(
        canary_module,
        "_run_telegram_operator_response_probe",
        lambda options: {
            "ok": True,
            "nonce": "op-rfq",
            "prompt": "rfq",
            "evidence": {
                "status": "pass",
                "mode": "signed_operator_response_probe",
                "latency_ms": 500,
                "latency_budget_ms": 10_000,
                "content_match": True,
                "telegram_send_ok": True,
                "nonce": "op-rfq",
                "prompt": "rfq",
                "repo_sha": current_sha[:12],
                "created_at": time.time(),
            },
        },
    )

    result = canary_module._canary_telegram_operator_response(options)

    assert result.status == PASS
    assert result.details["evidence_validation"]["checks"]["prompt_match"] is True
    assert result.details["evidence_validation"]["checks"]["runtime_sha_current"] is True


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


def test_gemini_frontier_probe_retries_after_timeout(monkeypatch, tmp_path):
    options = CanaryOptions(
        repo_root=Path(__file__).resolve().parents[2],
        hermes_home=tmp_path,
        gateway_url="",
        env_wrapper=None,
        frontier_eval=True,
        require_live=True,
        timeout=0.2,
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(canary_module, "_gemini_model_candidates", lambda: ["gemini-2.5-pro", "gemini-2.5-flash"])

    def fake_model_probe(*, model, **_kwargs):
        if model == "gemini-2.5-pro":
            return {
                "provider": "gemini",
                "model": model,
                "ok": False,
                "error_type": "TimeoutError",
                "summary": "Gemini frontier probe failed: TimeoutError",
            }
        return {
            "provider": "gemini",
            "model": model,
            "ok": True,
            "summary": "gemini-2.5-flash Gemini wrapper passed structured reasoning probe in 1000ms",
        }

    monkeypatch.setattr(canary_module, "_run_gemini_model_probe", fake_model_probe)

    result = canary_module._run_gemini_frontier_probe(options)

    assert result["ok"] is True
    assert result["model"] == "gemini-2.5-flash"
    assert [attempt["model"] for attempt in result["model_attempts"]] == ["gemini-2.5-pro", "gemini-2.5-flash"]


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
