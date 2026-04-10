# SDA-WIRE Operational Runbook

> The SDA-WIRE integration wires the Summit Domain Addon (SDA) stack into the live
> auto-quote pipeline via a pure bridge module. This runbook is the
> production-facing operational reference: architecture, data flow, troubleshooting,
> rollback, and known limitations.

---

## 1. Architecture Overview

SDA-WIRE connects eight SDA tools into the existing `AutoQuoteEngine` without
rewriting the pricing path. The bridge is a pure function — all I/O happens at
the caller level or via injected probes — so the live pipeline remains fully
functional even if every SDA tool fails.

### Files in the wire-up

| File | Role |
|---|---|
| `tools/auto_quote_bridge.py` | Pure orchestration — `BridgeContext`, `BridgeResult`, `run_bridge`. Never raises; all failures surface as `warnings`. |
| `tools/no_price_cascade.py` | Four-tier pricing cascade: V11 engine → Summit sheet → Gmail history → outbound solicit → manual Telegram. |
| `tools/summit_sheet_tool.py` | Summit 8140 XLSX lookup with Jorge Fernandez email override merging. Privacy-walled (`privacy=high`). |
| `tools/summit_trace_flags.py` | Per-line trace flag emitter: Summit consignment detection, `'145'`/`'8130'` trace type, IDG warning banner. |
| `tools/customer_quote_ref.py` | Regex extraction of customer quote references from RFQ body text. |
| `tools/quote_append_detector.py` | Matches incoming RFQs against recent open quotes with confidence grading (HIGH/MED/LOW/NONE). |
| `tools/outbound_solicit_tool.py` | Builds price-free solicit drafts for non-Summit vendors. Summit recipients are hard-excluded by default. |
| `tools/telegram_sda_flows.py` | Sync callback handlers for the 4 Telegram approval surfaces (no-price, solicit, append, IDG). |
| `~/.hermes/services/ils_auto_quote.py` (loose file) | Production pipeline. Modified by SWA-002 with a 9-line best-effort bridge call + fallback. |
| `gateway/platforms/telegram.py` | Registers `^sda_` CallbackQueryHandler routing to `_handle_sda_callback`. |

### Hard invariants (enforced in code and verified by domain_rules tests)

| Invariant | Enforced where |
|---|---|
| Privacy wall: Summit sheet content never leaves the Mac Studio. | `tools/summit_sheet_tool.py::assert_local_only` |
| V11 engine output is immutable ground truth — LLMs never generate a numeric price. | Cascade architecture; no LLM in the price path |
| Summit contacts (`@summitmro.com`) excluded from default outbound solicits. | `tools/outbound_solicit_tool.py::_is_summit_email` + `check_manual_recipient_override` |
| IDG Summit piece parts produce a yellow warning banner, NEVER a hard lock. | `tools/summit_trace_flags.py::emit_trace_flags` → `idg_piece_part_warning` |
| AR condition lines never carry `'8130 on request'`. | `tools/summit_trace_flags.py` trace_type rules + `tests/integration/test_sda_e2e.py` |
| Summit-sourced lines cite `'145 trace, Summit Aerospace tags'`. | `tools/summit_trace_flags.py` tag_source + guidance |
| Bridge falls back gracefully — any failure leaves `ils_auto_quote.py` unchanged. | `tools/auto_quote_bridge.py` per-step try/except + top-level safety net |

---

## 2. Data Flow

```
┌──────────────────┐
│  ILS RFQ ingress │
│ (loose file poll)│
└────────┬─────────┘
         │
         ▼
┌───────────────────────────────────┐
│ AutoQuoteEngine._process_rfq      │
│   matched/unmatched stock loop    │
└────────┬──────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────────┐
│ run_bridge(BridgeContext, engine/history/solicit/manual probes│
│            open_quotes)                                       │
│                                                               │
│   Step 1: extract_customer_quote_ref(rfq_body)                │
│   Step 2: detect_append_suggestion ───┐                       │
│              HIGH/MED ──► short-circuit, return early         │
│              LOW ──► record and continue                      │
│              NONE ──► continue                                │
│   Step 3: for each PN:                                        │
│              run_cascade                                      │
│                 engine → sheet (summit_sheet_lookup)          │
│                        → history → solicit → manual           │
│   Step 4: for each priced PN:                                 │
│              emit_trace_flags (per-line, isolated)            │
│                                                               │
│   All steps wrapped in try/except → warnings[]                │
│   Never raises past this boundary.                            │
└────────┬──────────────────────────────────────────────────────┘
         │
         ▼
┌───────────────────────────────┐
│ BridgeResult                  │
│   cascade_results[]           │
│   trace_flags[]               │
│   customer_quote_ref          │
│   append_suggestion           │
│   warnings[]                  │
└────────┬──────────────────────┘
         │
         ▼
┌───────────────────────────────┐
│ ils_auto_quote.py continues   │
│   draft quote creation        │
│   V11 SO creation             │
│   ils_notifications.send      │
└────────┬──────────────────────┘
         │
         ▼
┌───────────────────────────────┐
│ Telegram HITL surfaces        │
│   ^sda_noprice_ — manual qty  │
│   ^sda_solicit_ — vendor email│
│   ^sda_append_  — append/new  │
│   ^sda_idg_     — IDG dismiss │
└───────────────────────────────┘
```

---

## 3. How to Enable / Disable the Bridge

The bridge is **enabled by default** via the loose-file wire-up. To disable:

### Temporary disable (no restart)

Set the environment variable below before the gateway starts:

```bash
export HERMES_SDA_WIRE_DISABLED=1
```

> Note: this flag is NOT yet honored in the SWA-002 insertion. Implementing it
> is a 2-line follow-up: wrap the `run_bridge` call in
> `if not os.getenv("HERMES_SDA_WIRE_DISABLED"):`. Track as a future task.

### Full rollback (forced bypass)

See [Rollback Procedure](#6-rollback-procedure) below.

---

## 4. Troubleshooting — Grep-able Error Patterns

| Error pattern in logs | Likely cause | Fix |
|---|---|---|
| `SDA bridge failed on RFQ ... (fallback):` | `run_bridge` raised an uncaught exception (should be impossible given per-step catches). | Inspect the following log line for the traceback. Open a ticket. The pipeline is still working in fallback mode. |
| `SDA bridge warnings on RFQ ...` | Bridge succeeded but at least one step logged a warning. Not fatal. | Inspect the `warnings[]` for the specific step (cascade failed for pn=..., trace flags failed for pn=..., etc.). |
| `cascade failed for pn=...` | `run_cascade` raised for one PN. Other PNs processed normally. | Check the probe callables — an engine/history/solicit/manual probe may have misbehaved. |
| `trace flags failed for pn=...` | `emit_trace_flags` raised for one PN. Other priced lines still get flags. | Likely a missing field in the Summit sheet row. Check `summit_sheet_lookup(pn)` output directly. |
| `Jorge email fetch: UNEXPECTED exception for ...` | `_fetch_jorge_emails` hit an exception OUTSIDE the named transient modes. Parser bug or google client API change. | Inspect the traceback. This surface was narrowed in SWA-005 specifically to make parser regressions visible. |
| `Jorge cost sanity rejected for ... jorge=... kent=... ratio=` | SWA-005 sanity check rejected a Jorge override that was >3x or <1/3 the Kent Ext Cost baseline. | Verify the Jorge email content — this is usually a parser glitch or a legitimate "over_cost" sell-side advice. To accept, set `HERMES_JORGE_COST_SANITY_RATIO=10.0` (or higher). |
| `duplicate pn skipped: ...` | Caller passed the same PN twice. Only the first occurrence is processed. | Review the upstream RFQ parser — duplicate PNs in an RFQ are almost always a bug. |
| `SDA callback handler error: ...` | Telegram dispatcher or one of the 4 handlers raised. | The placeholder `_noop` deps should never raise; a real traceback here means the downstream wiring (future story) is broken. |
| `Error processing SDA callback` | User-facing toast when the dispatcher fails. | Check `SDA callback handler error` log line for the underlying cause. |

### 4 most likely failure modes

1. **Bridge raises during run_cascade** — symptom: `cascade failed for pn=X` warning, cascade_results missing that PN. Fix: inspect the probe callables. The bridge's guarantee is that ONE PN failing never blocks the others.

2. **Gmail auth expired** — symptom: `_fetch_jorge_emails` returns `[]` with DEBUG log `expected transient failure for ... (RefreshError)`. Fix: re-authorize Jorge Gmail credentials and verify `JORGE_GMAIL_TOKEN_PATH` exists. No impact on the pipeline — Summit sheet entries fall back to Kent Ext Cost.

3. **Summit sheet missing on disk** — symptom: `summit_sheet_lookup` returns `None` for every Summit PN. Cascade falls through to history → solicit → manual. Fix: verify the sheet path resolves (`check_summit_requirements()`). The sheet path defaults to `~/alexandria/advanced/operations/summit_8140_pricing.xlsx` or whatever `SUMMIT_SHEET_PATH` env var resolves.

4. **Telegram callback lost** — symptom: user clicks an approval button, nothing happens. Check: (a) the gateway process is running; (b) `^sda_` pattern is registered in `gateway/platforms/telegram.py`; (c) the `_handle_sda_callback` method is present. The dispatcher itself never raises; the issue is upstream.

---

## 5. Observability Touch Points

| Signal | Location | What it tells you |
|---|---|---|
| `log.warning("SDA bridge warnings on RFQ %s: %s")` | `~/.hermes/logs/gateway.log` | The bridge ran successfully but at least one step logged a warning. Non-blocking. |
| `log.warning("SDA bridge failed on RFQ %s (fallback): %s")` | `~/.hermes/logs/gateway.log` | The bridge raised and the existing pricing path is being used as fallback. Investigate. |
| `logger.warning("Jorge cost sanity rejected for %s: ...")` | `~/.hermes/logs/gateway.log` | SWA-005 sanity check fired. Usually benign. |
| `logger.warning("Jorge email fetch: UNEXPECTED exception ...")` | `~/.hermes/logs/gateway.log` | SWA-005 safety net fired. Investigate — this is rare and indicates a real bug. |
| `logger.error("SDA callback handler error: %s")` | `~/.hermes/logs/gateway.log` | The Telegram dispatcher or a handler raised. Should not happen with the current `_noop` deps. |
| Telegram alerts | `@ACthecollector` via `TELEGRAM_BOT_TOKEN_CZAR` | Ralph session notifications per story. Not wired yet for production alerts — future work. |
| Provenance dict on BridgeResult cascade_results | In-process only | Each cascade result carries a `provenance` dict describing where the cost_basis came from (engine, sheet, history, etc.) and any Jorge override metadata. Useful for post-mortems. |

---

## 6. Rollback Procedure

If SDA-WIRE misbehaves in production and you need to revert to the pre-bridge
behavior:

### Step 1: stop the gateway

```bash
# Find and stop the Hermes gateway process
pkill -f "python.*gateway/run.py"
```

### Step 2: restore the loose file from backup

The SWA-002 backup lives inside this repo at a fixed path:

```bash
cp ~/.hermes/hermes-agent/tasks/backup-ils_auto_quote-2026-04-10.py \
   ~/.hermes/services/ils_auto_quote.py
```

Verify the restore worked:

```bash
diff ~/.hermes/hermes-agent/tasks/backup-ils_auto_quote-2026-04-10.py \
     ~/.hermes/services/ils_auto_quote.py
# Expect empty output.
```

### Step 3: restart the gateway

```bash
cd ~/.hermes/hermes-agent
./start_gateway.sh
```

### Notes

- The bridge module at `tools/auto_quote_bridge.py` can remain in place. It is
  only invoked by the loose-file wire-up; once the import/call lines are gone,
  the bridge is dormant code with zero runtime cost.
- All other SDA tools (summit_sheet_tool, trace flags, etc.) also remain in
  place. They are independent of the wire-up.
- `tools/telegram_sda_flows.py` and the `^sda_` CallbackQueryHandler in
  `gateway/platforms/telegram.py` can also remain. The handler only fires if a
  user clicks an `sda_` button, and those buttons only exist if the pipeline
  creates them. With the loose file reverted, no `sda_` buttons are created,
  so the handler is dormant.

---

## 7. Known Limitations

1. **Jorge email parser is format-sensitive.** Gemini 2.5 Pro's 2026-04-10 audit
   flagged `_parse_jorge_cost` as brittle to format changes. SWA-005 added the
   Jorge cost sanity check as a guard, but the underlying parser can still miss
   new formats. If Jorge changes his email style, expect warning logs and
   reduced override rates until the parser is updated.

2. **Real-sheet integration tests gated off default CI.** Tests marked
   `@pytest.mark.integration` only run with `-m integration` and are not part
   of the default `pytest` invocation. CI does not currently run them.

3. **Deferred integration test files.** The following three test files were
   scoped into the original SDA PRD but deferred and still not written:
   `tests/integration/test_quote_append_integration.py`,
   `tests/integration/test_verify_engine_live.py`,
   `tests/integration/test_outbound_solicit_smtp.py`. Track as follow-up work.

4. **Telegram wire-up requires the gateway to be running.** The
   `_handle_sda_callback` method is registered during `TelegramAdapter.start()`.
   If the gateway is not running, no `sda_` callbacks are handled — the pipeline
   still produces draft quotes but the HITL approval surfaces are inert.

5. **Downstream wiring is placeholder.** The `_handle_sda_callback` method uses
   `_noop` deps (confirmed by SWA-003). Real downstream side effects — SMTP send,
   V11 quote append, manual price queue — are not yet wired. Clicking an SDA
   button today logs a confirmation but does NOT actually send an email, append
   a line, or trigger any V11 write. Those wirings are scoped as follow-ups.

6. **SWA-002 line budget is tight.** The loose-file insertion is 9 code lines
   under a 10-line ceiling. Any future change to the bridge call site must
   either stay within that budget or explicitly re-scope the constraint.

---

## 8. Reference

- PRD: `tasks/prd-sda-wire.json`
- Prerequisite PRD: `tasks/prd-summit-domain-addons.json` (SDA-001 through SDA-008, completed 2026-04-10)
- Ralph progress log: `tasks/progress.txt`
- Overnight context: `tasks/overnight-run-2026-04-10.md`
- Backup for SWA-002 rollback: `tasks/backup-ils_auto_quote-2026-04-10.py`
- E2E acceptance test: `tests/integration/test_sda_e2e.py`
- Hard-rule compliance suite: `tests/integration/test_domain_rules.py`

---

*Runbook generated as the final deliverable of SWA-006. Last updated 2026-04-10.*
