"""Tests for AAC-specific Alexandria/V11 context routing."""

import json
import subprocess

from gateway import context_router


def test_message_requests_alexandria_context_keywords():
    for term in (
        context_router.CONTEXT_TERMS
        + context_router.V11_TERMS
        + context_router.HERMES_TERMS
    ):
        assert context_router.message_requests_alexandria_context(f"please check {term}")


def test_message_requests_alexandria_context_part_number():
    for part_number in ("345789-1", "AB1234-5", "12345"):
        assert context_router.message_requests_alexandria_context(f"check stock for {part_number}")


def test_message_requests_alexandria_context_diagnostics_bypass():
    for message in ("ping", "status", "/health", "reply exactly OK"):
        assert not context_router.message_requests_alexandria_context(message)


def test_part_number_false_positive_from_url_id_avoided():
    assert not context_router.message_requests_alexandria_context(
        "https://example.com/page/12345"
    )


def test_collect_alexandria_context_returns_source_paths(monkeypatch):
    monkeypatch.setattr(
        context_router,
        "_run_alexandria_context_command",
        lambda args, timeout=20.0: json.dumps(
            {"results": [{"rel_path": "advanced/pricing/CONTEXT.md"}]}
        ),
    )
    monkeypatch.setattr(context_router, "_read_alexandria_source_snippets", lambda paths: "")
    monkeypatch.setattr(
        context_router,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=5000: "",
    )

    result = context_router.collect_alexandria_context("pricing context for AAC")

    assert "advanced/pricing/CONTEXT.md" in result["source_paths"]


def test_collect_alexandria_context_uses_lsh_before_non_expanding_qmd(monkeypatch):
    calls = []

    def _fake_run(args, timeout=20.0):
        calls.append(args)
        if args[0] == "alex-search":
            return json.dumps({"results": [{"rel_path": "advanced/czar/CONTEXT.md"}]})
        if args[0] == "qmd":
            return "qmd://alexandria/advanced/operations/CONTEXT.md:1\nHermes operations"
        return ""

    monkeypatch.delenv("HERMES_BRAIN_QMD_MODE", raising=False)
    monkeypatch.setattr(context_router, "_run_alexandria_context_command", _fake_run)
    monkeypatch.setattr(context_router, "_read_alexandria_source_snippets", lambda paths: "")
    monkeypatch.setattr(
        context_router,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=5000: "",
    )

    result = context_router.collect_alexandria_context("look in Alexandria for Hermes")

    assert calls[0][0] == "alex-search"
    assert calls[1][:2] == ["qmd", "search"]
    assert ["qmd", "query"] not in [call[:2] for call in calls]
    assert result["retrieval_plan"]["order"][:2] == ["alex-search", "qmd-search"]
    assert result["retrieval_plan"]["qmd_expansion_default"] is False
    assert "advanced/czar/CONTEXT.md" in result["source_paths"]
    assert "advanced/operations/CONTEXT.md" in result["source_paths"]


def test_brain_retrieval_health_reports_freshness_and_order(monkeypatch, tmp_path):
    now = 1_700_000_000.0
    fresh_index = tmp_path / "fresh.msgpack"
    stale_index = tmp_path / "stale.msgpack"
    fresh_index.write_text("fresh", encoding="utf-8")
    stale_index.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(context_router.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(
        context_router,
        "lsh_daemon_health",
        lambda: {"running": True, "responsive": True, "alex_docs": 10, "v11_products": 20},
    )
    monkeypatch.delenv("HERMES_BRAIN_QMD_MODE", raising=False)
    old_time = now - 30 * 86400
    fresh_time = now - 2 * 86400
    import os

    os.utime(fresh_index, (fresh_time, fresh_time))
    os.utime(stale_index, (old_time, old_time))

    health = context_router.brain_retrieval_health(
        now=now,
        max_age_days=14,
        index_paths={
            "alexandria_lsh": fresh_index,
            "v11_lsh": fresh_index,
            "qmd": stale_index,
        },
    )

    assert health["retrieval_order"] == ["alex-search", "qmd-search", "v11-search"]
    assert health["lsh_first"] is True
    assert health["daemon"]["responsive"] is True
    assert health["qmd_expansion_default"] is False
    assert health["stale_indexes"] == ["qmd"]


def test_collect_alexandria_context_retrieval_succeeded_flag(monkeypatch):
    monkeypatch.setattr(context_router, "_read_alexandria_source_snippets", lambda paths: "")
    monkeypatch.setattr(
        context_router,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=5000: "",
    )
    monkeypatch.setattr(
        context_router,
        "_run_alexandria_context_command",
        lambda args, timeout=20.0: "qmd://alexandria/advanced/czar/CONTEXT.md:1\nStudio target",
    )

    succeeded = context_router.collect_alexandria_context("look in Alexandria for Hermes")

    assert succeeded["retrieval_succeeded"] is True
    assert "retrieval was attempted, but no source output" not in succeeded["context_text"]

    monkeypatch.setattr(
        context_router,
        "_run_alexandria_context_command",
        lambda args, timeout=20.0: "",
    )

    failed = context_router.collect_alexandria_context("look in Alexandria for Hermes")

    assert failed["retrieval_succeeded"] is False


def test_collect_alexandria_context_handles_subprocess_timeout(monkeypatch):
    def _timeout(args, timeout=20.0):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=timeout)

    monkeypatch.setattr(context_router, "_run_alexandria_context_command", _timeout)
    monkeypatch.setattr(context_router, "_read_alexandria_source_snippets", lambda paths: "")
    monkeypatch.setattr(
        context_router,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=5000: "",
    )

    result = context_router.collect_alexandria_context("look in Alexandria")

    assert result["retrieval_succeeded"] is False
    assert any("timed out" in error for error in result["errors"])


def test_direct_context_paths_for_pricing_keyword():
    assert (
        "advanced/pricing/CONTEXT.md"
        in context_router.direct_alexandria_context_paths("pricing strategy")
    )


def test_direct_context_paths_for_non_rfq_business_lanes():
    finance_paths = context_router.direct_alexandria_context_paths(
        "cash receivables payables invoice exposure"
    )
    assert "advanced/financials/CONTEXT.md" in finance_paths
    assert "advanced/operations/CONTEXT.md" in finance_paths

    purchasing_paths = context_router.direct_alexandria_context_paths(
        "vendor followups and open purchase order blockers"
    )
    assert "advanced/operations/CONTEXT.md" in purchasing_paths
    assert "advanced/vendor-pricing/CONTEXT.md" in purchasing_paths

    repair_paths = context_router.direct_alexandria_context_paths(
        "stuck repair teardown cert blocker"
    )
    assert "advanced/operations/CONTEXT.md" in repair_paths
    assert "advanced/teardown/CONTEXT.md" in repair_paths


def test_direct_context_paths_routing_md_always_first():
    assert context_router.direct_alexandria_context_paths("pricing strategy")[0] == "_system/ROUTING.md"
