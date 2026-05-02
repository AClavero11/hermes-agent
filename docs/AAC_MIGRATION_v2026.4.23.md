# AAC Migration Inventory for `v2026.4.23`

## Workspace

| Item | Value |
|---|---|
| Clean clone | `/Users/ac/.hermes/hermes-agent-v2026.4.23` |
| Branch | `aac/migrate-v2026.4.23` |
| Release tag | `v2026.4.23` |
| Release commit | `bf196a3f` |
| Live AAC repo | `/Users/ac/.hermes/hermes-agent` |
| Divergence | `4700` release-side commits vs `48` AAC-side commits |
| Common ancestor | `caab1cf4` (`2026-03-07`) |

## Decision

Treat this as a migration workspace, not an in-place upgrade.

Reason:
- AAC has repo-level customizations.
- AAC also has substantial runtime logic outside git under `~/.hermes/`.
- The live AAC worktree is dirty, so it contains changes that are not safely attributable to either upstream or the AAC branch baseline.

## Do Not Copy Blindly

Hold these out of the migration until a specific step requires them:

- `~/.hermes/.env`
- `~/.hermes/google_client_secret.json`
- `~/.hermes/google_token.json`
- `~/.hermes/state.db`
- `~/.hermes/state.db.czar-backup`
- `~/.hermes/logs/`
- `~/.hermes/sessions/`
- `~/.hermes/audio_cache/`
- `~/.hermes/image_cache/`
- `~/.hermes/document_cache/`
- `~/.hermes/auth.lock`
- `~/.hermes/cc-sessions.json`
- `~/.hermes/pairing/`

## Port Order

1. Stand up the release repo as-is from this clean clone.
2. Port runtime home files that AAC depends on but which are not part of the repo.
3. Port AAC repo-tracked changes in small layers.
4. Triage the live dirty-worktree files separately.
5. Smoke-test CLI, gateway boot, MCP, and ILS auto-quote in an isolated `HERMES_HOME`.

## Runtime Files Outside Git

These are AAC-specific runtime assets that live under `~/.hermes/` and must be considered independently from the repo.

### Required Early

- `~/.hermes/config.yaml`
- `~/.hermes/SOUL.md`
- `~/.hermes/memories/MEMORY.md`
- `~/.hermes/memories/USER.md`
- `~/.hermes/litellm_config.yaml`
- `~/.hermes/mcp_servers/alexandria_server.py`
- `~/.hermes/mcp_servers/hermes_server.py`
- `~/.hermes/mcp_servers/v11_server.py`
- `~/.hermes/mcp_servers/v11_server.sh`
- `~/.hermes/scripts/bootstrap.sh`
- `~/.hermes/scripts/start-gateway.sh`
- `~/.hermes/scripts/start-litellm.sh`
- `~/.hermes/scripts/watchdog.sh`
- `~/.hermes/scripts/model-keepwarm.sh`
- `~/.hermes/scripts/alexandria-reindex.sh`
- `~/.hermes/scripts/qmd-sync.sh`
- `~/.hermes/scripts/on-wake.sh`
- `~/.hermes/scripts/sync-agent-memories.sh`

### Required For AAC Automation

- `~/.hermes/services/gipl_lookup.py`
- `~/.hermes/services/gmail_draft.py`
- `~/.hermes/services/ils_auto_quote.py`
- `~/.hermes/services/ils_notifications.py`
- `~/.hermes/services/inspect_unanswered_rfqs.py`
- `~/.hermes/services/market_scanner.py`
- `~/.hermes/services/quote_approval_bot.py`
- `~/.hermes/services/quote_memory.py`
- `~/.hermes/services/quote_notify.py`
- `~/.hermes/services/quote_pdf.py`
- `~/.hermes/services/quote_pdf_server.py`
- `~/.hermes/services/rfq_batch_triage.py`
- `~/.hermes/services/shipment_watcher.py`
- `~/.hermes/services/data/ils_quotes.db`
- `~/.hermes/services/data/rfq-session-handoff.md`
- `~/.hermes/services/data/shipments.db`

### Required If Keeping Current Preprocessing / Redaction Path

- `~/.hermes/preprocessor/compressor.py`
- `~/.hermes/preprocessor/config.yaml`
- `~/.hermes/preprocessor/search_client.py`
- `~/.hermes/preprocessor/server.py`
- `~/.hermes/preprocessor/start-preprocessor.sh`
- `~/.hermes/sanitizer/__init__.py`
- `~/.hermes/sanitizer/audit.py`
- `~/.hermes/sanitizer/cli.py`
- `~/.hermes/sanitizer/config.py`
- `~/.hermes/sanitizer/config.yaml`
- `~/.hermes/sanitizer/detector.py`
- `~/.hermes/sanitizer/middleware.py`
- `~/.hermes/sanitizer/patterns.py`
- `~/.hermes/sanitizer/qwen_fallback.py`
- `~/.hermes/sanitizer/redactor.py`
- `~/.hermes/sanitizer/sanitize.py`
- `~/.hermes/sanitizer/requirements.txt`
- `~/.hermes/sanitizer_callback.py`

### Optional Runtime Assets

- `~/.hermes/workflows/daily_summary.yaml`
- `~/.hermes/workflows/stock_check.yaml`
- `~/.hermes/workflows/turkish_photo_approval.yaml`
- `~/.hermes/templates/approval_package.html`
- `~/.hermes/knowledge/index_metadata.json`
- `~/.hermes/skills/ui-design-brain.md`
- `~/.hermes/scripts/ai_tweet_monitor.py`

## AAC Repo-Tracked Delta to Port

These are the tracked AAC branch files relative to the shared ancestor. Port them in layers.

### Layer 1: Identity, Prompting, and Profiles

- `AGENTS.md`
- `CLAUDE.md`
- `agent/prompt_builder.py`
- `hermes_cli/main.py`
- `hermes_cli/profile.py`
- `hermes_state.py`
- `cli.py`
- `run_agent.py`
- `agent/context_compressor.py`
- `model_tools.py`
- `pyproject.toml`
- `requirements-dev.txt`
- `tests/test_cli_init.py`
- `tests/test_hermes_state.py`
- `tests/test_profile.py`
- `tests/tools/test_clipboard.py`

### Layer 2: Gateway and Telegram Wiring

- `gateway/config.py`
- `gateway/platforms/telegram.py`
- `gateway/run.py`
- `gateway/session.py`
- `gateway/status.py`
- `cron/scheduler.py`
- `tools/send_message_tool.py`
- `tools/registry.py`
- `toolsets.py`

### Layer 3: Summit / SDA / Auto-Quote Domain Logic

- `tools/__init__.py`
- `tools/auto_quote_bridge.py`
- `tools/cc_remote.py`
- `tools/customer_quote_ref.py`
- `tools/mcp_tool.py`
- `tools/memory_tool.py`
- `tools/no_price_cascade.py`
- `tools/outbound_solicit_tool.py`
- `tools/quote_append_detector.py`
- `tools/summit_sheet_tool.py`
- `tools/summit_trace_flags.py`
- `tools/telegram_sda_flows.py`
- `scripts/__init__.py`
- `scripts/verify_engine_capabilities.py`
- `docs/sda-wire-runbook.md`

### Layer 4: Test Fixtures and Validation

- `tests/fixtures/domain_rules/mock_gmail_mailbox.json`
- `tests/fixtures/domain_rules/mock_v11_responses.json`
- `tests/fixtures/domain_rules/sample_rfqs.json`
- `tests/integration/test_bridge_wire.py`
- `tests/integration/test_domain_rules.py`
- `tests/integration/test_sda_e2e.py`
- `tests/integration/test_summit_sheet_integration.py`
- `tests/integration/test_summit_trace_flow.py`
- `tests/integration/test_telegram_sda_integration.py`
- `tests/scripts/__init__.py`
- `tests/scripts/test_verify_engine_capabilities.py`
- `tests/tools/test_auto_quote_bridge.py`
- `tests/tools/test_customer_quote_ref.py`
- `tests/tools/test_no_price_cascade.py`
- `tests/tools/test_outbound_solicit.py`
- `tests/tools/test_quote_append_detector.py`
- `tests/tools/test_summit_sheet_tool.py`
- `tests/tools/test_summit_sheet_tool_sanity.py`
- `tests/tools/test_telegram_sda_flows.py`
- `tests/tools/test_trace_flags.py`
- `.vulture-whitelist.py`

### Layer 5: Task and Planning Artifacts

Keep only if they are still useful operationally. They are not runtime-critical.

- `tasks/backup-ils_auto_quote-2026-04-10.py`
- `tasks/overnight-run-2026-04-10.md`
- `tasks/prd-karpathy-ops.json`
- `tasks/prd-sda-wire.json`
- `tasks/prd-summit-domain-addons.json`
- `tasks/prd.json`
- `tasks/progress-karpathy-ops.txt`
- `tasks/progress.txt`
- `tasks/ralph-spec-swa-001.md`
- `tasks/ralph-spec-swa-003.md`
- `tasks/ralph-spec-us003.md`
- `tasks/ralph-spec-us004.md`
- `tasks/ralph-spec-us005.md`
- `tasks/ralph-state.json`
- `tasks/test-matrix.md`
- `skills/social-media/xurl/SKILL.md`

## Conflict Watchlist

These files are touched both by upstream `v2026.4.23` and by AAC local changes. Port them manually, not by blind copy:

- `run_agent.py`
- `gateway/run.py`
- `agent/context_compressor.py`
- `gateway/platforms/telegram.py`
- `tools/registry.py`
- `hermes_cli/main.py`
- `cli.py`
- `pyproject.toml`

These files are dirty in the live worktree and should not be used as an authoritative source until reviewed:

- `.github/workflows/tests.yml`
- `.gitignore`
- `agent/context_compressor.py`
- `batch_runner.py`
- `gateway/run.py`
- `run_agent.py`
- `tools/patch_parser.py`

## Live Dirty-Worktree Files to Triage Separately

These are present only in the live AAC worktree right now. Do not port them automatically into the migration branch without an explicit review pass.

- `.pre-commit-config.yaml`
- `INDEX.md`
- `config/`
- `docs/ACTIVATION_CHECKLIST.md`
- `docs/ACTIVATION_SUMMARY.md`
- `docs/CLAUDE_INTEGRATION.md`
- `docs/DISCORD_SETUP.md`
- `docs/GATEWAY_MULTI_PLATFORM.md`
- `docs/IMPLEMENTATION_SUMMARY.md`
- `docs/MCP_SERVER_README.md`
- `docs/PLATFORMS_INDEX.md`
- `docs/PLATFORMS_OVERVIEW.md`
- `docs/QUICKSTART_CLAUDE.md`
- `docs/QUICKSTART_CLAUDE_CODE.md`
- `docs/SLACK_SETUP.md`
- `docs/security/`
- `gateway/url_enrichment.py`
- `hermes_api/`
- `hermes_cli/invoke.py`
- `hermes_cli/pipelines.py`
- `hermes_cli/stats.py`
- `hermes_sdk/`
- `knowledge/`
- `mcp_audit_logger.py`
- `mcp_input_validator.py`
- `mcp_policy.py`
- `mcp_server.py`
- `observability/`
- `pipelines/`
- `requirements-api.txt`
- `start_gateway.sh`
- `tasks/prd-karpathy-ops.md`
- `tasks/ralph-spec-SDA-001.md`
- `tasks/ralph-spec-us001.md`
- `tasks/ralph-spec-us002.md`
- `tests/test_hermes_api.py`
- `tests/test_hermes_sdk.py`
- `tests/test_mcp_security.py`
- `tests/test_mcp_server.py`
- `tests/test_security_integration.py`
- `tests/tools/test_cc_remote.py`
- `tests/tools/test_claude_reasoning.py`
- `tests/tools/test_environments.py`
- `tests/tools/test_mcp_tool_approval.py`
- `tools/claude_reasoning_tool.py`
- `tools/content_sanitizer.py`
- `tools/jina_web_tool.py`
- `tools/mcp_tool_approval.py`
- `tools/pipeline_tool.py`
- `tools/unified_search_tool.py`
- `tools/x_scraper_tool.py`
- `validate_platforms.py`
- `verify_mcp_protocol.py`

## Suggested Smoke-Test Order

1. Use a fresh `HERMES_HOME`, not the live `~/.hermes/`.
2. Boot CLI only.
3. Verify profile resolution and prompt construction.
4. Verify MCP server boot.
5. Verify gateway import and start path.
6. Verify Telegram callback path.
7. Verify `ils_auto_quote.py` import path and dry-run logic.
8. Run the Summit / SDA test suite before any live messaging or V11 actions.

## Immediate Next Step

Start porting Layer 1 into this clean clone, then run the smallest possible local smoke test before touching gateway or ILS automation.
