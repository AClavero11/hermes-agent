# Hermes Control Plane Patch Manifest - 2026-05-02

Tag target: `hermes-control-plane-2026-05-02`
Source repo: `/Users/ac/.hermes/hermes-agent-v2026.4.23`
Pre-freeze base SHA: `4f36f30d2a84a88d3e5f890eada6d91855873a8f`

## Repo Patch Stack

These files define the reproducible Hermes control-plane patch stack:

| Path | Purpose |
|---|---|
| `agent/prompt_builder.py` | Prompt/context behavior for AAC local DeepSeek path |
| `gateway/context_router.py` | Alexandria/V11 context routing |
| `gateway/platforms/api_server.py` | OpenAI-compatible API behavior and no-tool eval path |
| `gateway/platforms/base.py` | Gateway platform contract support |
| `gateway/platforms/telegram.py` | Telegram webhook/security behavior |
| `gateway/run.py` | Gateway goals, Workspace capture, direct status/canary answers |
| `hermes_cli/canary.py` | Canary/eval/readiness harness |
| `hermes_cli/commands.py` | Slash command registry |
| `hermes_cli/goals.py` | Durable goal state |
| `hermes_cli/main.py` | CLI command wiring |
| `hermes_cli/runtime_status.py` | Runtime resolver: launchd cwd, selected repo, Python, model route, health URL, git SHA |
| `hermes_cli/workspace.py` | Workspace task/evidence/report ledger |
| `run_agent.py` | Agent runtime integration |
| `toolsets.py` | Toolset registration |
| `tools/workspace_tool.py` | Workspace tool |
| `tools/x_scraper_tool.py` | X/Twitter source retrieval tool |
| `tests/gateway/test_api_server.py` | API behavior coverage |
| `tests/gateway/test_status_command.py` | Status/direct answer coverage |
| `tests/hermes_cli/test_canary.py` | Canary harness coverage |
| `tests/tools/test_x_scraper_tool.py` | X scraper tool coverage |
| `docs/AAC_LOCAL_DEEPSEEK.md` | Local DeepSeek operating notes |
| `docs/AAC_MIGRATION_v2026.4.23.md` | Migration notes |
| `docs/HERMES_SELF_HEAL_PLAYBOOKS.md` | Bounded self-heal policy |
| `docs/patch-manifests/hermes-control-plane-2026-05-02.md` | This manifest |
| `scripts/deploy-hermes-control-plane.sh` | Exact-file deploy script for local-to-Studio sync and runtime SHA metadata |
| `scripts/hermes-canary-daily` | Daily canary wrapper with signed Telegram webhook simulation |

## External Runtime Files

These files live outside the repo and are copied by `scripts/deploy-hermes-control-plane.sh`:

| Local path | Studio target |
|---|---|
| `/Users/ac/.hermes/services/service_env.py` | `/Users/anthonyclavero/.hermes/services/service_env.py` |
| `/Users/ac/.hermes/services/ils_auto_quote.py` | `/Users/anthonyclavero/.hermes/services/ils_auto_quote.py` |
| `/Users/ac/.hermes/services/market_scanner.py` | `/Users/anthonyclavero/.hermes/services/market_scanner.py` |
| `/Users/ac/.hermes/services/quote_approval_bot.py` | `/Users/anthonyclavero/.hermes/services/quote_approval_bot.py` |
| `/Users/ac/.hermes/services/quote_notify.py` | `/Users/anthonyclavero/.hermes/services/quote_notify.py` |
| `/Users/ac/.hermes/services/quote_pdf.py` | `/Users/anthonyclavero/.hermes/services/quote_pdf.py` |
| `/Users/ac/.hermes/services/quote_pdf_server.py` | `/Users/anthonyclavero/.hermes/services/quote_pdf_server.py` |
| `/Users/ac/.hermes/services/shipment_watcher.py` | `/Users/anthonyclavero/.hermes/services/shipment_watcher.py` |
| `/Users/ac/.hermes-deepseek/bin/hermes-env.sh` | `/Users/anthonyclavero/.hermes-deepseek/bin/hermes-env.sh` |
| `scripts/hermes-canary-daily` | `/Users/anthonyclavero/.hermes-deepseek/bin/hermes-canary-daily` |

## Deploy Targets

The deploy script syncs the repo patch stack to both active Studio checkouts:

| Target | Path |
|---|---|
| Versioned runtime | `/Users/anthonyclavero/.hermes/hermes-agent-v2026.4.23` |
| Active runtime | `/Users/anthonyclavero/.hermes/hermes-agent` |

## Verification Gates

Run these after applying the manifest:

```bash
python -m py_compile hermes_cli/runtime_status.py hermes_cli/main.py hermes_cli/commands.py hermes_cli/canary.py
bash -n scripts/deploy-hermes-control-plane.sh
pytest -o addopts='' tests/hermes_cli/test_canary.py tests/gateway/test_api_server.py tests/gateway/test_status_command.py
hermes runtime status --json
hermes canary --require-live --live-behavior --reasoning-eval --frontier-eval --json
```

Every canary JSON/Markdown report now records a `runtime` object with the runtime repo SHA and wrapper-selected repo.
For rsync-only Studio trees without `.git`, the deploy script writes `.hermes-runtime-version.json` so `hermes runtime status` and canary reports still record the source SHA.
The daily canary runs `--telegram-webhook-sim` so the Telegram gate can be exercised through the signed webhook path without requiring a visible DM for every scheduled run.
