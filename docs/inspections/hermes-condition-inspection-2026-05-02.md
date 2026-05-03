# Hermes Condition Inspection - 2026-05-02

Inspection basis: live Studio runtime, local source repo, launchd state, listeners, logs, canary reports, targeted tests, and secret-pattern scan.

## Executive Score

| Score type | Score | Meaning |
|---|---:|---|
| Measured canary readiness | 9.7/10 | Latest full Studio canary is `365/365 (100.0%)`, `frontier_ready 9/9`; current deploy SHA is recorded in `latest.json` |
| Operational condition | 8.4/10 | Serviceable for guarded operator work; not yet overhauled for fully autonomous business operation |
| Business automation ceiling today | 9.7/10 | Can prepare guarded RFQ/quote packages; cannot yet perform approved customer-send + audit/follow-up loop |

Current condition: serviceable with discrepancies.

## Verified Evidence

| Evidence | Result |
|---|---|
| Source repo | Clean after inspection/report commit |
| Studio runtime | `/Users/anthonyclavero/.hermes/hermes-agent-v2026.4.23`, deploy metadata records the current source SHA, dirty=false |
| Gateway health | `http://127.0.0.1:8643/health` returns `{"status":"ok","platform":"hermes-agent"}` |
| Model endpoints | `8090` and `8091` both list Coder Lite and DeepSeek V4 Flash |
| Direct DeepSeek eval | `5/5`, `30/30`; arithmetic cases `69`, `5650`, `39000` pass |
| Frontier wrapper | Gemini `gemini-2.5-pro` structured probe passed; OpenAI quota remains separate |
| Telegram webhook | Telegram API reports webhook URL `https://hermes.advanced.aero/telegram`, pending updates `0`, no last error |
| Quote PDF server | `http://127.0.0.1:8699/health` returns OK |
| Focused tests | `176 passed`, `120 warnings` |
| Compile/shell checks | `py_compile` and `bash -n` pass |
| Secret scan | No live token/key pattern found in tracked repo or AAC service files; hits were test/docs placeholders |

## Subsystem Scores

| Area | Score | Condition | Evidence | Discrepancy / overhaul need |
|---|---:|---|---|---|
| Live Studio runtime | 8.8 | Serviceable | Launchd gateway running, health OK, runtime SHA recorded | Launchd cwd is unversioned checkout while wrapper selects versioned repo; acceptable but still drift-prone |
| Model and inference | 8.6 | Serviceable | DeepSeek V4 Flash route active; direct reasoning now `5/5`; Gemini frontier wrapper passes | Local model still needs scaffold/verifier for arithmetic; MLX V4 support uses temporary branch/fork; no separate executor/judge/synthesizer route populated |
| Gateway/API | 8.3 | Serviceable with discrepancy | `8643` OpenAI-compatible API healthy; API/status/Telegram tests pass | `8642` org health surface is not listening despite prior docs/context references; decide restore or remove references |
| Telegram | 8.5 | Serviceable | Webhook info clean; signed E2E passes; visible delivery previously proved by inbound `ack` | Daily canary skips human-visible delivery; webhook keeper log had transient `set_error HTTPError` before subsequent restores |
| Eval harness | 9.0 | Strong | Full canary `365/365`; trend/history files exist; goldens cover RFQ, quote, Telegram, model, safety | Canary launch agent installed but `launchctl` reports `runs = 0`; latest full run was manually invoked scheduled path |
| Workspace/control plane | 8.8 | Serviceable | Workspace task/evidence/report checks pass; Goal link hooks exist | Needs stricter "no done without evidence" enforcement for real work, not only canary contract |
| Quote/RFQ ops | 8.2 | Guarded serviceable | Quote ops runtime `8/8`; RFQ dry-run `14/14`; approved draft `13/13`; QAMFORM preview works | Still guarded payload mode: no production V11/Atlas write and no customer-facing send |
| Safety/security | 8.6 | Serviceable | Approval bot customer-send blocked; external sends guarded; secret scan clean | General email platform still exists and needs policy-level routing audit before 10.0 customer-send automation |
| Observability/logs | 7.6 | Marginal serviceable | Logs and scorecards exist; latest scorecard clean | Logs are noisy and partly unstructured; no alerting dashboard; no launchd-fired daily log yet |
| Deployment/config drift | 7.4 | Needs overhaul | Deploy script writes runtime metadata; Studio clean | Local MacBook launchd/runtime is stale: `8643` refused locally, state file points to old `feishu` pytest process, only local `8443` listens |
| Code health/tests | 8.0 | Serviceable | Targeted tests pass | Scoped ruff finds existing F401/F841 issues in `gateway/run.py` and older gateway tests; warnings are high |
| Alexandria/memory continuity | 8.4 | Serviceable | Hermes context, patterns, corrections updated | Needs a single active runtime truth table that reconciles Studio/local/Alexandria docs automatically |

## Hard Findings

| Finding | Severity | Evidence | Required action |
|---|---|---|---|
| Local MacBook Hermes runtime drift | High | Local launchd says running, but `8643` health refused; state file shows stale pytest `feishu` process from 2026-04-27 | Either disable local gateway or make it a real mirror with clean state, health, and same scorecard contract |
| `8642` API surface drift | Medium | Studio `8642/health` connection refused while older context describes `8642` as org health | Decide: restore `8642` or delete stale docs/tests/context references |
| Daily canary launchd not yet proven by launchd | Medium | `ai.hermes.deepseek-canary` installed but `runs = 0`; latest report was manual scheduled-path run | Trigger one launchd run or wait for 06:20, then verify stdout/stderr logs and history entry |
| Business loop stops before customer send | Medium | Latest 9.7 canary explicitly uses `write_mode=guarded_payload_only` | Build 10.0 approved-send workflow with audit proof, V11/Atlas write proof, follow-up, and rollback evidence |
| Observability is report-first, alert-light | Medium | JSON/Markdown scorecards exist; no operator dashboard/alert policy yet | Add health dashboard and alert thresholds after runtime drift is removed |
| Lint/warnings debt | Low | `176 passed`, but ruff finds 9 F401/F841 issues; pytest emits 120 warnings | Clean scoped lint debt and replace aiohttp string app keys with `web.AppKey` over time |

## Overhaul Work Packages

| Priority | Package | Acceptance criteria |
|---|---|---|
| P0 | Runtime drift overhaul | One authoritative Studio runtime, one rollback, local gateway disabled or healthy mirror, no stale state files, `8642` decision closed |
| P0 | Scheduled canary proof | Launchd-triggered daily canary produces logs and latest report without manual invocation |
| P1 | Model routing overhaul | Explicit planner/executor/judge/synthesizer routes; local DeepSeek for bounded work, frontier/judge for ambiguous business decisions, deterministic calculator/verifier for numeric work |
| P1 | 10.0 business-send gate | Approved quote can create production draft/write, send customer email only after approval, write audit artifact, schedule follow-up, and surface exceptions |
| P1 | Observability overhaul | Single dashboard/report showing services, ports, model latency, Telegram webhook, canary trend, failures, and next action |
| P2 | Code health cleanup | Ruff zero for Hermes-owned touched files, pytest warning budget reduced, stale docs reconciled |
| P2 | Telegram human path | Weekly or manual visible-delivery probe with inbound ack evidence before relying on customer-critical approvals |

## Bottom Line

Hermes is currently fit for guarded internal operator workflows and RFQ/quote package preparation.

Hermes is not yet fit for unattended customer-facing operation because the customer-send/audit/follow-up loop is intentionally not built, and runtime drift remains between Studio and local.

Next overhaul step: close P0 runtime drift first, then build the 10.0 approved-send gate.
