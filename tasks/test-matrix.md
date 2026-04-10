# CZAR-to-Hermes Migration — Test Matrix
**Author:** QA Strategist (Swarm Worker) | **Date:** 2026-03-17
**Source plan:** /tmp/swarm-review.md (Inspector-corrected final plan)
**Test ID format:** T-{step}-{NNN}

---

## Summary

| Step | Unit Tests | Integration Tests | Smoke Tests | Rollback Tests | Total |
|------|-----------|------------------|-------------|----------------|-------|
| Pre-Migration | 0 | 0 | 4 | 0 | 4 |
| E1 (V11Client) | 8 | 3 | 2 | 1 | 14 |
| E2 (Pricing) | 9 | 4 | 2 | 2 | 17 |
| E3 (Email Gate) | 7 | 4 | 3 | 2 | 16 |
| E4 (ILS AQ) | 6 | 4 | 3 | 2 | 15 |
| E5 (AEX AQ) | 5 | 4 | 3 | 2 | 14 |
| E6 (Email RFQ) | 5 | 4 | 3 | 2 | 14 |
| E7 (Quote Delivery) | 5 | 3 | 2 | 1 | 11 |
| E8 (Win/Payment) | 6 | 4 | 3 | 2 | 15 |
| E10 (Integration) | 0 | 8 | 5 | 3 | 16 |
| **Total** | **51** | **38** | **30** | **17** | **136** |

---

## Pre-Migration Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-PRE-001 | smoke | Verify Hermes bot token identity | Token belongs to @HermesAC_bot, not MONITORS bot | `curl -s "https://api.telegram.org/bot$(grep TELEGRAM_BOT_TOKEN ~/.hermes/.env | cut -d= -f2)/getMe" \| python3 -m json.tool` | `result.username == "HermesAC_bot"` |
| T-PRE-002 | smoke | Verify CZAR bot token on EC2 is different | CZAR uses a different token (or same, inform Option 2 decision) | `ssh ubuntu@<ec2> "grep TELEGRAM_TOKEN /home/ubuntu/czar_bot/.env"` | Returns token; confirm if same first 10 chars as Hermes |
| T-PRE-003 | smoke | Verify gateway plist is running | `ai.hermes.gateway` is loaded and running | `launchctl list ai.hermes.gateway` | PID is non-zero; no `-1` exit code |
| T-PRE-004 | smoke | Verify certs directory needs creating | `~/.hermes/services/certs/` does not exist yet | `ls ~/.hermes/services/certs/` | Error "No such file or directory" → create it; if exists, verify contents |

---

## E1: V11Client Extraction + Resilience

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E1-001 | unit | `authenticate()` with valid credentials | Returns non-zero integer UID | `python3 -c "from services.v11_client import V11Client; c=V11Client(); print(c.authenticate())"` | `uid > 0` (typically 2 or 3) |
| T-E1-002 | unit | `authenticate()` with invalid password | Raises `V11AuthError` or returns 0/False | Set `ODOO_PASSWORD=wrongpass` in env, run authenticate() | Exception raised or falsy return; no silent success |
| T-E1-003 | unit | Retry logic: mock V11 failure, verify 3 retries | 3 attempts logged then exception raised | `python3 tests/test_v11_client.py::test_retry_on_failure` | Log shows `attempt 1/3`, `attempt 2/3`, `attempt 3/3` before final exception |
| T-E1-004 | unit | Retry backoff timing: delays between attempts | Delays are 1s, 2s, 4s (exponential) | Mock `time.sleep`, assert call args in `test_retry_backoff` | `sleep` called with `[1, 2, 4]` in sequence |
| T-E1-005 | unit | `find_part("762367B")` returns correct product data | Returns dict with `id`, `name`, `qty_available`, `list_price` | `python3 -c "from services.v11_client import V11Client; c=V11Client(); print(c.find_part('762367B'))"` | `result['name'] == '762367B'`; `qty_available >= 0` |
| T-E1-006 | unit | `find_part("NOTEXIST999")` for unknown part | Returns `None` or empty list | Same as T-E1-005 but with bogus part number | `result is None` or `result == []`; no exception |
| T-E1-007 | unit | `find_customer("Advanced Air")` fuzzy matching | Returns best-match partner record | `python3 -c "from services.v11_client import V11Client; c=V11Client(); print(c.find_customer('Advanced Air'))"` | Returns partner with `id > 0`; name contains "Advanced" |
| T-E1-008 | unit | `find_customer("")` with empty string | Returns None or raises ValueError cleanly | `c.find_customer("")` | No V11 XML-RPC call made (guard clause); None returned |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E1-009 | integration | `ils_auto_quote.py` imports V11Client from `v11_client.py` after refactor | ILS poll runs without ImportError | `python3 -c "from services.ils_auto_quote import AutoQuoteEngine; e=AutoQuoteEngine(); print('OK')"` | Prints `OK`; no ImportError |
| T-E1-010 | integration | V11Client XML-RPC call sequence: authenticate then search | Single session UID reused across calls | Check V11 access logs or add call counter: `c.authenticate(); c.find_part("762367B"); assert c._call_count == 2` | UID fetched once; subsequent calls use cached UID |
| T-E1-011 | integration | Circuit-breaker: V11 unreachable for 3 consecutive polls, then recovers | After 3 failures, circuit opens; after recovery, resets to closed | `python3 tests/test_v11_client.py::test_circuit_breaker_open_and_reset` | Calls blocked after 3 failures; `circuit_open == True`; after mock recovery `circuit_open == False` |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E1-012 | smoke | `v11_client.py` imports cleanly | No syntax errors | `python3 -c "import services.v11_client; print('OK')"` | Prints `OK` |
| T-E1-013 | smoke | ILS poll still works post-refactor | No regression; quotes still created | Check gateway.log after one tick cycle (5 min) | No `ImportError` or `AttributeError` in gateway.log |

### Rollback Test

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E1-014 | rollback | Revert `ils_auto_quote.py` to inline V11Client | ILS poll resumes with inlined client | `git stash; python3 -c "from services.ils_auto_quote import AutoQuoteEngine"` | Imports cleanly from stash-reverted file |

---

## E2: Pricing Intelligence

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E2-001 | unit | Age adjustment: part sold 1 year ago vs 5 years ago | 5-year-old price adjusted less aggressively than broken formula | `python3 tests/test_pricing_intelligence.py::test_age_adjustment_not_over_inflating` | Price adjustment for 5-yr-old sale ≤ 1.5x original; not 5-10x |
| T-E2-002 | unit | Customer weighting: same-customer sale vs general history | Same-customer sale ranks first | `python3 tests/test_pricing_intelligence.py::test_customer_weighting` | `result.pricing_method == "CUSTOMER_HISTORY"` when same-customer sale exists |
| T-E2-003 | unit | Floor stacking: margin floor applies to cost basis only | Floor is `cost * floor_multiplier`, not `inflated_fmv * floor_multiplier` | `python3 tests/test_pricing_intelligence.py::test_floor_applied_to_cost_not_fmv` | `floor_price == unit_cost * TIER_MARGIN_FLOORS[tier]`; not `fmv * ...` |
| T-E2-004 | unit | Outlier rejection: single 10x sale excluded from average | Part numbers 743631, 755350, 766091 outliers removed | `python3 tests/test_pricing_intelligence.py::test_outlier_rejection` | Average computed without outlier; price within 2x of median |
| T-E2-005 | unit | `compute_fmv("762367B", qty=1, cost=500)` returns value in plausible range | FMV between $750 and $1500 (1.5x-3x cost for IDG part) | `python3 -c "from services.pricing_intelligence import compute_fmv; print(compute_fmv('762367B', 1, 500))"` | `750 <= price <= 1500` |
| T-E2-006 | unit | `compute_fmv` with zero cost falls back gracefully | Returns `NEEDS_MANUAL` method, not crash | `compute_fmv("762367B", 1, 0)` | Returns `(0, "NEEDS_MANUAL")` |
| T-E2-007 | unit | `compute_fmv` with no sale history | Falls through to cost markup | Mock `get_sale_history` returns `[]`; call `compute_fmv` | `pricing_method == "COST_MARKUP"` |
| T-E2-008 | unit | Turso DB connection: `get_sale_history("762367B")` | Returns list of historical sales | `python3 -c "from services.pricing_intelligence import get_sale_history; print(get_sale_history('762367B'))"` | Returns list (may be empty); no `libsql` connection error |
| T-E2-009 | unit | FMV waterfall DRY_RUN flag: no V11 SO created when true | Pricing computed but `create_sale_order` not called | `AUTO_QUOTE_DRY_RUN=true python3 tests/test_pricing_intelligence.py::test_dry_run_no_create` | `create_sale_order` call count == 0 |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E2-010 | integration | Backtest: 50 real V11 SOs — error distribution | >70% within 10%, <5% red flags (>3x) | `cd ~/.hermes && python3 scripts/backtest_pricing.py --engine fmv --limit 50` | Stdout shows `within_10pct >= 70%` and `red_flags <= 5%` |
| T-E2-011 | integration | Backtest: compare FMV output vs simpler `compute_price()` on same 50 SOs | FMV ≥ simple engine accuracy; if not, simple engine stays default | `python3 scripts/backtest_pricing.py --engine both --limit 50` | FMV `within_10pct >= compute_price within_10pct` |
| T-E2-012 | integration | Pricing fallback: if FMV waterfall raises, falls back to `compute_price()` | No unhandled exception; price returned via fallback | In `pricing_intelligence.py` mock `compute_fmv` to raise `RuntimeError`; run ILS poll | ILS poll completes; `pricing_method == "COST_MARKUP"` in notification |
| T-E2-013 | integration | Turso sync: `scripts/sync_turso_pricing.py` populates DB from V11 | Turso `sale_history` table has rows for known parts | `python3 scripts/sync_turso_pricing.py --dry-run` then `python3 scripts/sync_turso_pricing.py --limit 100` | `SELECT COUNT(*) FROM sale_history` > 0 after run |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E2-014 | smoke | `pricing_intelligence.py` imports cleanly | No missing deps | `python3 -c "import services.pricing_intelligence; print('OK')"` | Prints `OK` |
| T-E2-015 | smoke | Ported CZAR tests pass | No regressions from CZAR test suite | `cd ~/.hermes && python3 -m pytest tests/test_pricing_intelligence.py -v` | All tests pass (green) |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E2-016 | rollback | Remove `pricing_intelligence.py`, ILS poll falls back to `compute_price()` | ILS auto-quote continues working with simple pricing | `mv services/pricing_intelligence.py /tmp/; restart gateway; send test ILS RFQ` | Quote created with `pricing_method == "COST_MARKUP"`; no error in logs |
| T-E2-017 | rollback | If backtest fails (<70%), `AUTO_QUOTE_DRY_RUN=true` blocks real quoting | No V11 draft SOs created in production | Set `AUTO_QUOTE_DRY_RUN=true` in `.env`; trigger ILS poll | `grep "DRY_RUN" ~/.hermes/logs/gateway.log` shows dry-run message; V11 SO count unchanged |

---

## E3: Email Gate

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E3-001 | unit | `queue_email(to, subject, body, attachments)` stores row in SQLite | Row exists in `email_queue` table with `status='pending'` | `python3 -c "from services.email_gate import queue_email; qid=queue_email('test@test.com','subj','body'); print(qid)"` | Returns integer `qid > 0`; `SELECT * FROM email_queue WHERE id=qid` returns row |
| T-E3-002 | unit | `queue_email` Telegram notification sent after enqueue | Telegram `send_message` called with APPROVE/REJECT buttons | Mock `bot.send_message`; call `queue_email()`; assert mock called | Mock called once with `reply_markup` containing `InlineKeyboardButton` |
| T-E3-003 | unit | APPROVE callback `emailgate_approve_{qid}` triggers Gmail API send | `gmail_api.send()` called with correct args | Mock Gmail API; send `emailgate_approve_1` callback; assert mock called | `gmail_api.send` called with `to`, `subject`, `body` matching queued email |
| T-E3-004 | unit | REJECT callback `emailgate_reject_{qid}` marks rejected, no send | `status='rejected'` in DB; Gmail API NOT called | Mock Gmail API; send `emailgate_reject_1` callback; assert mock not called | `SELECT status FROM email_queue WHERE id=1` == `'rejected'`; Gmail mock call count == 0 |
| T-E3-005 | unit | No direct Gmail send path exists outside `email_gate.py` | Grep finds no `gmail` import outside `email_gate.py` and `quote_delivery.py` (which calls `email_gate`) | `grep -r "gmail\|smtplib\|sendmail" ~/.hermes/services/ --include="*.py" \| grep -v email_gate` | Zero results; all Gmail sends routed through gate |
| T-E3-006 | unit | `queue_email` with PDF attachment: attachment stored or referenced correctly | Attachment data preserved for send step | `queue_email("t@t.com", "s", "b", attachments=[("/tmp/test.pdf", "test.pdf")])`; check DB row | `attachments_json` column contains path reference; file exists at path |
| T-E3-007 | unit | Double-approve protection: second APPROVE on same `qid` is no-op | Status already `'sent'`; Gmail API not called a second time | Mock Gmail; call `handle_approve_callback(qid)` twice | Gmail mock called exactly once; second call returns early |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E3-008 | integration | CallbackQueryHandler registered for `emailgate_` pattern in telegram.py | Gateway handles `emailgate_approve_1` callback | Start gateway; send Telegram callback with data `emailgate_approve_1` via BotFather test | Callback answer sent; no "unhandled callback" log error |
| T-E3-009 | integration | Full approve flow: queue → Telegram message → click APPROVE → email sent | Email arrives at test address | `queue_email("ac+test@advanced.aero", "Test", "Body")`; click APPROVE in Telegram | Email received at test address within 60s |
| T-E3-010 | integration | Gmail SA JSON loaded from `~/.hermes/services/certs/gmail-sa.json` | Auth succeeds; no FileNotFoundError | `python3 -c "from services.email_gate import _init_gmail; _init_gmail(); print('OK')"` | Prints `OK` |
| T-E3-011 | integration | `certs/` directory exists with correct files before E3 starts | Both Gmail SA and Teller certs present | `ls -la ~/.hermes/services/certs/` | `gmail-sa.json`, `teller_certificate.pem`, `teller_private_key.pem` all present |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E3-012 | smoke | `email_gate.py` imports cleanly | No missing deps | `python3 -c "import services.email_gate; print('OK')"` | Prints `OK` |
| T-E3-013 | smoke | SQLite DB created automatically on first import | `email_queue.db` exists in data dir | Run T-E3-012; then `ls ~/.hermes/services/data/email_queue.db` | File exists |
| T-E3-014 | smoke | `emailgate_` callback pattern registered in telegram.py | Pattern visible in handler list | `grep "emailgate_" ~/.hermes/hermes-agent/gateway/platforms/telegram.py` | Pattern string found |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E3-015 | rollback | Remove `email_gate.py`: quote delivery fails safely | `quote_delivery.py` raises `ImportError` with clear message; no direct Gmail fallback | `mv services/email_gate.py /tmp/`; trigger quote delivery | `ImportError: email_gate required`; no email sent |
| T-E3-016 | rollback | Pending queue survives gateway restart | Queued emails still pending after gateway restart | Queue 2 emails; restart gateway; check DB | `SELECT COUNT(*) FROM email_queue WHERE status='pending'` == 2 |

---

## E4: ILS Auto-Quote Enhancement

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E4-001 | unit | `_run_ils_poll()` added to `_start_cron_ticker()` hardcoded loop | Function called every 5 ticks | `grep "_run_ils_poll" ~/.hermes/hermes-agent/gateway/run.py` | Line present inside `_start_cron_ticker` while-loop body |
| T-E4-002 | unit | `ILS_POLL_EVERY` constant is 5 (every 5 minutes) | Value matches existing pattern | `grep "ILS_POLL_EVERY" ~/.hermes/hermes-agent/gateway/run.py` | `ILS_POLL_EVERY = 5` |
| T-E4-003 | unit | Notification dedup: same RFQ ID not notified twice | Second notification for same rfq_id suppressed | `python3 tests/test_notification_gate.py::test_dedup_same_rfq_id` | Second `send_quote_notification()` call returns `None` (suppressed) |
| T-E4-004 | unit | Notification rate limit: max 10 notifications per hour | 11th notification in 60 minutes suppressed | `python3 tests/test_notification_gate.py::test_rate_limit_10_per_hour` | 11th call returns `None`; log shows rate limit hit |
| T-E4-005 | unit | DRY_RUN=true: `poll_and_process()` runs but `create_sale_order` not called | Results returned; V11 unchanged | `AUTO_QUOTE_DRY_RUN=true python3 tests/test_ils_auto_quote.py::test_dry_run` | `create_sale_order` mock call count == 0; results list non-empty |
| T-E4-006 | unit | FMV waterfall wired in: `ILSAutoQuoteEngine` calls `compute_fmv` not `compute_price` | After E2 passes backtest, pricing upgraded | `grep "compute_fmv\|pricing_intelligence" ~/.hermes/services/ils_auto_quote.py` | `compute_fmv` import present; `compute_price` still as fallback |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E4-007 | integration | ILS poll detects new RFQ: full pipeline (poll→stock check→draft SO→notification) | Draft SO created in V11; Telegram notification with APPROVE/REJECT | Manually create ILS test RFQ or replay fixture; run `_run_ils_poll()` | V11 SO in draft state; Telegram message received by AC (chat ID 496461229) |
| T-E4-008 | integration | APPROVE callback creates confirmed quote in V11 | SO status changes from draft | Click APPROVE on Telegram notification; check V11 | V11 query: `models.execute_kw(db, uid, pw, 'sale.order', 'read', [[so_id]], {'fields': ['state']})` returns `'sale'` or `'draft'` (depending on flow) |
| T-E4-009 | integration | REJECT callback marks SO as cancelled in V11 and quote DB | V11 SO cancelled; local DB status='rejected' | Click REJECT; check V11 + local DB | V11 SO state == `'cancel'`; `SELECT status FROM quotes WHERE rfq_id=... ` == `'rejected'` |
| T-E4-010 | integration | No double-quote: CZAR ILS disabled, Hermes ILS enabled, single quote per RFQ | Exactly 1 SO per RFQ in V11 | Set `FEATURES["auto_quote_ils"] = False` on EC2; enable Hermes ILS poll; send 5 test RFQs; query V11 | V11 query: `models.execute_kw(db, uid, pw, 'sale.order', 'search_count', [[['partner_id', '=', partner_id], ['state', '=', 'draft']]])` == 5 (not 10) |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E4-011 | smoke | Gateway log shows ILS poll every 5 minutes | Heartbeat visible in logs | `tail -f ~/.hermes/logs/gateway.log` for 10 minutes | `ILS poll` log line appears at 5-min and 10-min marks |
| T-E4-012 | smoke | No ILS-related errors in first 24h | Zero ILS errors after cutover | `grep -c "ERROR.*ils" ~/.hermes/logs/gateway.log` | Count == 0 |
| T-E4-013 | smoke | CZAR ILS poll disabled: no new SOs from EC2 | CZAR feature flag confirmed off | `ssh ubuntu@<ec2> "grep auto_quote_ils /home/ubuntu/czar_bot/config.py"` | `FEATURES["auto_quote_ils"] = False` confirmed |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E4-014 | rollback | Comment out `_run_ils_poll()` call in `_start_cron_ticker()`, re-enable CZAR | ILS auto-quote resumes from EC2 | Comment line in `run.py`; restart gateway; set `FEATURES["auto_quote_ils"] = True` on EC2; wait 5 min | CZAR log shows ILS poll; Hermes log shows no ILS poll |
| T-E4-015 | rollback | Rollback time target | Process completes in under 10 minutes | Time the full rollback: comment + restart + EC2 re-enable | Clock ≤ 10 min from decision to both systems stable |

---

## E5: AeroXchange Auto-Quote

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E5-001 | unit | `aex_auto_quote.py` imports cleanly, all deps present | No ImportError | `python3 -c "import services.aex_auto_quote; print('OK')"` | Prints `OK` |
| T-E5-002 | unit | `_run_aex_poll()` hardcoded in `_start_cron_ticker()` loop | Function called every N ticks (match CZAR's interval) | `grep "_run_aex_poll" ~/.hermes/hermes-agent/gateway/run.py` | Line present inside while-loop; `AEX_POLL_EVERY` constant defined |
| T-E5-003 | unit | AEX RFQ detection: poll returns new RFQ from test fixture | `poll_rfqs()` returns list with test RFQ | `AUTO_QUOTE_DRY_RUN=true python3 tests/test_aex_auto_quote.py::test_poll_returns_rfq` | List len > 0; first item has `rfq_id`, `parts` |
| T-E5-004 | unit | `aexq_` callback pattern registered in telegram.py | CallbackQueryHandler with `aexq_` pattern added | `grep "aexq_" ~/.hermes/hermes-agent/gateway/platforms/telegram.py` | Pattern string found in handler registration |
| T-E5-005 | unit | DRY_RUN=true: AEX poll runs but no V11 SO created | Results returned; `create_sale_order` not called | `AUTO_QUOTE_DRY_RUN=true python3 tests/test_aex_auto_quote.py::test_dry_run` | Mock call count == 0 |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E5-006 | integration | AEX full pipeline: poll→V11 stock check→draft SO→Telegram notification | Telegram notification arrives with `aexq_approve/reject` buttons | Set `AUTO_QUOTE_DRY_RUN=false`; trigger AEX test RFQ; wait | Notification received; button data starts with `aexq_` |
| T-E5-007 | integration | APPROVE callback on AEX quote: V11 SO confirmed | SO in V11 changes state | Click APPROVE; verify V11 SO | Same V11 XML-RPC check as T-E4-008 |
| T-E5-008 | integration | No double-quote: CZAR AEX disabled, Hermes AEX active | Single SO per RFQ | Set `FEATURES["auto_quote_aex"] = False` on EC2; enable Hermes; send 3 test RFQs | V11 shows exactly 3 new SOs (not 6) |
| T-E5-009 | integration | AEROXCHANGE_USER / AEROXCHANGE_PASSWORD env vars loaded | No `KeyError` or empty-string auth failure | `python3 -c "import os; from dotenv import load_dotenv; load_dotenv('/Users/ac/.hermes/.env'); assert os.getenv('AEROXCHANGE_USER'), 'Missing'; print('OK')"` | Prints `OK` |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E5-010 | smoke | Gateway log shows AEX poll heartbeat | Heartbeat fires | `grep "aex.*poll\|AEX poll" ~/.hermes/logs/gateway.log \| tail -5` | Lines present with timestamps 5+ min apart |
| T-E5-011 | smoke | No AEX auth errors in first 24h | AeroXchange credentials valid | `grep "ERROR.*aex\|AeroXchange.*401\|auth.*fail" ~/.hermes/logs/gateway.log` | Count == 0 |
| T-E5-012 | smoke | CZAR AEX confirmed disabled on EC2 | No CZAR AEX activity | `ssh ubuntu@<ec2> "grep auto_quote_aex /home/ubuntu/czar_bot/config.py"` | `FEATURES["auto_quote_aex"] = False` |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E5-013 | rollback | Comment out `_run_aex_poll()` in cron ticker, re-enable CZAR AEX | AEX resumes on EC2 | Comment line in `run.py`; restart gateway; set CZAR flag True; wait 5 min | CZAR log shows AEX poll; Hermes does not |
| T-E5-014 | rollback | Rollback time target | ≤ 10 minutes | Time full rollback | Clock ≤ 10 min |

---

## E6: Email RFQ Auto-Quote

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E6-001 | unit | `email_rfq_scanner.py` imports cleanly | No ImportError | `python3 -c "import services.email_rfq_scanner; print('OK')"` | Prints `OK` |
| T-E6-002 | unit | `rfq_parser.py`: parse email body with known RFQ format | Returns `RFQPart` list with part numbers, quantities | `python3 -c "from services.rfq_parser import parse_rfq_email; parts=parse_rfq_email(open('tests/fixtures/rfq_sample.txt').read()); print(parts)"` | List len > 0; `parts[0].part_number` is non-empty string |
| T-E6-003 | unit | `emailq_` callback pattern registered in telegram.py | CallbackQueryHandler added | `grep "emailq_" ~/.hermes/hermes-agent/gateway/platforms/telegram.py` | Pattern string found |
| T-E6-004 | unit | `_run_email_rfq_poll()` hardcoded in `_start_cron_ticker()` | Function called per schedule | `grep "_run_email_rfq_poll" ~/.hermes/hermes-agent/gateway/run.py` | Line inside while-loop body |
| T-E6-005 | unit | DRY_RUN=true: email poll runs, no V11 SO created | `create_sale_order` not called | `AUTO_QUOTE_DRY_RUN=true python3 tests/test_email_rfq_scanner.py::test_dry_run` | Mock call count == 0 |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E6-006 | integration | Email RFQ full pipeline: inbound email→parse→V11 stock→draft SO→Telegram | Telegram notification with `emailq_` buttons | Send test RFQ to configured Gmail inbox; wait for poll; check Telegram | Notification received within 10 minutes of email send |
| T-E6-007 | integration | APPROVE callback on email RFQ: V11 SO confirmed, quote delivered via email gate | V11 SO created; email queued in gate | Click APPROVE in Telegram | V11 SO exists; `SELECT status FROM email_queue WHERE...` == `'pending'` awaiting second approval |
| T-E6-008 | integration | No double-quote: CZAR email RFQ disabled, Hermes active | Single SO per email RFQ | Set `FEATURES["auto_quote_email"] = False` on EC2; enable Hermes; send 3 test emails | V11 shows 3 new SOs (not 6) |
| T-E6-009 | integration | RFQ dedup: same email received twice (e.g., forwarded) | Only one SO created | Send identical RFQ email twice; wait two poll cycles | `SELECT COUNT(*) FROM email_rfq_queue WHERE rfq_hash='...'` == 1 SO in V11 |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E6-010 | smoke | `email_rfq_scanner.py` imports cleanly | No deps missing | `python3 -c "import services.email_rfq_scanner; print('OK')"` | Prints `OK` |
| T-E6-011 | smoke | Gateway log shows email RFQ poll heartbeat | Heartbeat fires | `grep "email.*rfq.*poll\|email_rfq" ~/.hermes/logs/gateway.log \| tail -5` | Lines present |
| T-E6-012 | smoke | Gmail API authorized for inbox read | No OAuth error on first poll | Check `gateway.log` after first email poll tick | No `google.auth` or `403` error in logs |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E6-013 | rollback | Comment out `_run_email_rfq_poll()` in cron ticker, re-enable CZAR email | Email RFQ resumes on EC2 | Comment + restart + EC2 re-enable; wait 10 min | CZAR log shows email poll; Hermes does not |
| T-E6-014 | rollback | Any queued email RFQ quotes survive rollback | Pending DB rows intact after restart | Check `email_rfq_queue` table before and after rollback | Row count unchanged |

---

## E7: Quote Delivery + PDF

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E7-001 | unit | PDF generation: `generate_quote_pdf(so_id)` produces valid PDF | File created, not empty, valid PDF header | `python3 -c "from services.quote_delivery import generate_quote_pdf; path=generate_quote_pdf(SO_ID); print(open(path,'rb').read(4))"` | Output starts with `b'%PDF'` |
| T-E7-002 | unit | PDF format matches CZAR: AAC header, part table, pricing | Field layout identical to CZAR output | Visual diff: generate PDF from same V11 SO in both CZAR and Hermes | Column order, company name, address match exactly |
| T-E7-003 | unit | `quote_delivery.py` calls `email_gate.queue_email()`, NOT Gmail API directly | No direct Gmail import | `grep "gmail\|smtplib" ~/.hermes/services/quote_delivery.py` | Zero results; only `from services.email_gate import queue_email` |
| T-E7-004 | unit | `auto_send.py` gates applied: 7 conditions checked before delivery | Each gate (grade, confidence, rate limit, stock, high-value, condition, anomaly) has test case | `python3 -m pytest tests/test_auto_send.py -v` | All 7 gate tests pass |
| T-E7-005 | unit | `auto_send.py` high-value gate: quotes >$50K held for manual review | Quote above threshold not auto-sent | `python3 tests/test_auto_send.py::test_high_value_gate` | `GateResult.held == True` for $50,001 quote |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E7-006 | integration | Full quote delivery: V11 SO → PDF → email gate → Telegram approve → email sent | Email with PDF attachment received | Run full E6 flow through to delivery approval | Email arrives at customer test address with PDF attachment |
| T-E7-007 | integration | `auto_send.py` DRY_RUN check: all 7 gates pass in DRY_RUN but no email sent | Gates evaluated; `queue_email` not called | `AUTO_QUOTE_DRY_RUN=true`; trigger quote delivery | `queue_email` mock call count == 0; gate pass log present |
| T-E7-008 | integration | PDF uses V11 `sale.report_saleorder` (not `website_quote`) per memory note | Report generated from correct Odoo template | Inspect PDF: check for "Quotation" vs "Sale Order" header; match expected template | Header matches `sale.report_saleorder` output format |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E7-009 | smoke | `quote_delivery.py` imports cleanly | No deps missing | `python3 -c "import services.quote_delivery; print('OK')"` | Prints `OK` |
| T-E7-010 | smoke | `auto_send.py` imports cleanly | No deps missing | `python3 -c "import services.auto_send; print('OK')"` | Prints `OK` |

### Rollback Test

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E7-011 | rollback | Remove `quote_delivery.py`: quotes still draft but not delivered | No unhandled exception; pipeline halts at delivery step with logged error | `mv services/quote_delivery.py /tmp/`; trigger approval; check logs | `ImportError` or `FileNotFoundError` logged; no email sent; V11 SO stays draft |

---

## E8: Win / Payment Alerts

### Unit Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E8-001 | unit | `win_alerts.py` imports cleanly | No missing deps | `python3 -c "import services.win_alerts; print('OK')"` | Prints `OK` |
| T-E8-002 | unit | `_run_win_check()` added to `_start_cron_ticker()` (every 15 ticks) | Constant defined; call in loop | `grep "_run_win_check\|WIN_CHECK_EVERY" ~/.hermes/hermes-agent/gateway/run.py` | Both lines present; `WIN_CHECK_EVERY = 15` |
| T-E8-003 | unit | `_run_payment_check()` added to `_start_cron_ticker()` (every 30 ticks) | Constant defined; call in loop | `grep "_run_payment_check\|PAYMENT_CHECK_EVERY" ~/.hermes/hermes-agent/gateway/run.py` | Both lines present; `PAYMENT_CHECK_EVERY = 30` |
| T-E8-004 | unit | Win alert V11 query: detects SO state change to `'sale'` | Returns list of newly confirmed SOs since last check | Mock V11 XML-RPC: `search_read` returns 1 SO with `state='sale'`; assert alert fires | `send_win_alert` called with correct SO data |
| T-E8-005 | unit | Win alert chat routing: sends to Shop channel, not Family | Chat ID matches Shop group | Check `win_alerts.py` constants: `SHOP_CHAT_ID` used for win alerts | `grep "SHOP_CHAT_ID\|shop_chat" ~/.hermes/services/win_alerts.py` returns correct ID |
| T-E8-006 | unit | Payment alert chat routing: sends to Family channel | Chat ID matches Family group | `grep "FAMILY_CHAT_ID\|family_chat" ~/.hermes/services/payment_alerts.py` returns correct ID | Correct chat ID constant used |

### Integration Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E8-007 | integration | Win alert end-to-end: create test SO in V11, confirm it, verify notification fires | Telegram message appears in Shop chat | In V11: `models.execute_kw(db, uid, pw, 'sale.order', 'action_confirm', [[test_so_id]])` then wait ≤15 ticks | Telegram message received in Shop chat with SO number and amount |
| T-E8-008 | integration | Payment alert: Teller mTLS connection from Mac Studio works | No cert error; connection established | `python3 -c "from services.payment_alerts import _test_teller_connection; _test_teller_connection(); print('OK')"` | Prints `OK`; no `ssl.SSLError` |
| T-E8-009 | integration | `teller_sync.py` dependency check: does `payment_alerts.py` import it? | Dependency clarified; if needed, both ported | `grep "teller_sync" ~/.hermes/services/payment_alerts.py` | If present: `teller_sync.py` also in services dir; if absent: payment_alerts self-contained |
| T-E8-010 | integration | `sales_tracker.py` tracks SO outcomes for pricing calibration | New won SO recorded in tracker DB | Confirm test SO (T-E8-007); check tracker DB | `SELECT COUNT(*) FROM outcomes WHERE so_id=test_so_id` == 1 |

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E8-011 | smoke | `payment_alerts.py` imports cleanly | No missing deps | `python3 -c "import services.payment_alerts; print('OK')"` | Prints `OK` |
| T-E8-012 | smoke | Teller certs present at expected path | mTLS certs accessible | `ls -la ~/.hermes/services/certs/teller_*.pem` | Both `teller_certificate.pem` and `teller_private_key.pem` present, non-zero size |
| T-E8-013 | smoke | Win check heartbeat fires in gateway log | `_run_win_check` visible in logs | `grep "win.*check\|win_check" ~/.hermes/logs/gateway.log \| tail -5` | Lines present at ~15-min intervals |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E8-014 | rollback | Comment out `_run_win_check()` and `_run_payment_check()` in cron ticker | No win/payment alerts from Hermes; no errors | Comment both lines; restart gateway; wait 30 min; check logs | No `win_check` or `payment_check` entries in log; no ERRORs |
| T-E8-015 | rollback | Teller mTLS fails: payment_alerts degrades gracefully | Error logged; no crash; other polls continue | Mock Teller endpoint to return `ssl.SSLError`; run `_run_payment_check()` | `ERROR payment_check: ...` in log; ILS/AEX polls unaffected |

---

## E10: Full Integration + DRY_RUN Validation

### Integration Tests (48-hour clean run)

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E10-001 | integration | ILS pipeline DRY_RUN: RFQ → V11 draft → Telegram notification → APPROVE/REJECT | All steps complete; V11 not modified (dry run) | Set `AUTO_QUOTE_DRY_RUN=true`; submit ILS test RFQ; click APPROVE in Telegram | Telegram notification received; V11 query for test part shows no new SO: `models.execute_kw(db, uid, pw, 'sale.order', 'search', [[['name', 'like', 'TEST-RFQ']]])` returns `[]` |
| T-E10-002 | integration | AEX pipeline DRY_RUN: same as T-E10-001 for AEX channel | Same | Submit AEX test RFQ; same verification | Same criteria |
| T-E10-003 | integration | Email RFQ DRY_RUN: email → parse → V11 stock check → Telegram → APPROVE | Same | Send test email RFQ; same verification | Same criteria |
| T-E10-004 | integration | Email gate: queue → Telegram → APPROVE → Gmail send (real test email) | Email delivered to test address | `queue_email("ac+test@advanced.aero", "Integration Test", "Body"); click APPROVE` | Email arrives at `ac+test@advanced.aero` within 60s |
| T-E10-005 | integration | Win alert integration: real V11 SO confirmed → Telegram | Notification in Shop chat | Confirm test SO in V11; wait ≤15 min | Telegram notification received |
| T-E10-006 | integration | No duplicate quotes: V11 audit query | Zero duplicate SOs for same partner+date+part | `python3 -c "import xmlrpc.client; ...` run duplicate audit query (see below) | Query returns 0 duplicates |
| T-E10-007 | integration | Pricing backtest final gate: >70% within 10%, <5% red flags | Backtest passes before DRY_RUN disabled | `python3 scripts/backtest_pricing.py --engine fmv --limit 50` | `within_10pct >= 70` and `red_flags <= 5` in output |
| T-E10-008 | integration | 48-hour error log check | Zero new ERRORs after clean start | `grep "ERROR" ~/.hermes/logs/gateway.log \| grep -v "known_benign"` after 48h | Count == 0 for new ERROR lines |

### V11 Duplicate SO Audit Query
```python
# Run after any DRY_RUN period to verify no accidental SO creation
import xmlrpc.client, os

url = os.getenv("ODOO_URL", "https://v11.advanced.aero")
db  = os.getenv("ODOO_DB",  "advancedaero")
uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(
        db, os.getenv("ODOO_USER"), os.getenv("ODOO_PASSWORD"), {})
models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

# Find SOs created in the last 24h in draft state — should be zero during DRY_RUN
from datetime import datetime, timedelta
cutoff = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
drafts = models.execute_kw(db, uid, os.getenv("ODOO_PASSWORD"),
    'sale.order', 'search_read',
    [[['state', '=', 'draft'], ['create_date', '>=', cutoff]]],
    {'fields': ['name', 'partner_id', 'create_date'], 'limit': 50})
print(f"Draft SOs in last 24h: {len(drafts)}")
for so in drafts:
    print(f"  {so['name']} | {so['partner_id'][1]} | {so['create_date']}")
```
**Pass criterion (DRY_RUN):** `len(drafts) == 0`
**Pass criterion (LIVE):** No partner+part duplicate within same calendar day

### Smoke Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E10-009 | smoke | All 4 callback patterns registered in telegram.py | `ilsq_`, `aexq_`, `emailq_`, `emailgate_` all present | `grep "ilsq_\|aexq_\|emailq_\|emailgate_" ~/.hermes/hermes-agent/gateway/platforms/telegram.py` | All 4 patterns appear |
| T-E10-010 | smoke | All 4 `_run_*` functions added to `_start_cron_ticker()` | Functions present and called in loop | `grep "_run_ils_poll\|_run_aex_poll\|_run_email_rfq_poll\|_run_win_check\|_run_payment_check" ~/.hermes/hermes-agent/gateway/run.py` | All 5 lines present inside `_start_cron_ticker` |
| T-E10-011 | smoke | `.env` has all required CZAR credentials | No missing key errors at import | `python3 -c "from dotenv import load_dotenv; import os; load_dotenv('/Users/ac/.hermes/.env'); missing=[k for k in ['AEROXCHANGE_USER','AEROXCHANGE_PASSWORD','TELLER_APP_ID','TURSO_DATABASE_URL','TURSO_AUTH_TOKEN','AUTO_QUOTE_DRY_RUN'] if not os.getenv(k)]; print('Missing:',missing)"` | Prints `Missing: []` |
| T-E10-012 | smoke | Gateway starts cleanly after all E-phase changes | No startup errors | `launchctl stop ai.hermes.gateway && launchctl start ai.hermes.gateway; sleep 10; grep "ERROR\|CRITICAL" ~/.hermes/logs/gateway.log \| tail -20` | Zero ERROR/CRITICAL lines in first 10s |
| T-E10-013 | smoke | CZAR still running on EC2 (cold backup) | EC2 service is UP | `ssh ubuntu@<ec2> "systemctl is-active hermes.service"` | Prints `active` |

### Rollback Tests

| Test ID | Type | Description | Expected Result | How to Run | Pass/Fail Criteria |
|---------|------|-------------|-----------------|------------|--------------------|
| T-E10-014 | rollback | Nuclear rollback: comment all new `_run_*` calls, re-enable all CZAR features | Full revert in ≤15 minutes | Comment 4 lines in `run.py`; restart gateway; SSH to EC2 and set all CZAR features to True | All CZAR logs show activity; Hermes log shows no auto-quote polls |
| T-E10-015 | rollback | Phase D rollback: `git checkout pre-strip-v1` on EC2 | CZAR back to pre-strip state | `ssh ubuntu@<ec2> "cd czar_bot && git checkout pre-strip-v1 && sudo systemctl restart hermes.service"` | CZAR starts; all features active; 5-minute confirmation |
| T-E10-016 | rollback | Verify rollback time target: nuclear in ≤15 min, Phase D in ≤5 min | Time-bounded rollbacks always possible | Dry-run the nuclear rollback on staging or document actual timing | Both time targets met |

---

## Test Execution Order

```
Pre-Migration (T-PRE-001 to T-PRE-004) — BLOCK on any failure
    ↓
E1 (T-E1-001 to T-E1-014)
    ↓
E2 (T-E2-001 to T-E2-017) — BLOCK on T-E2-010 backtest failure
    ↓
E3 (T-E3-001 to T-E3-016) — BLOCK on T-E3-005 gate integrity check
    ↓
E4 (T-E4-001 to T-E4-015) — channel cutover, 24h monitoring
    ↓
E5 (T-E5-001 to T-E5-014) — channel cutover, 24h monitoring
    ↓
E6 (T-E6-001 to T-E6-014) — channel cutover, 24h monitoring
    ↓
E7 (T-E7-001 to T-E7-011)
    ↓
E8 (T-E8-001 to T-E8-015)
    ↓
E10 (T-E10-001 to T-E10-016) — 48h clean run gate
```

**Hard blocks (migration cannot proceed past these):**
- T-PRE-001: Bot token unresolved → do not start Phase E
- T-E2-010: Backtest <70% within 10% → keep `AUTO_QUOTE_DRY_RUN=true`; do not flip to live
- T-E3-005: Any direct Gmail path found outside email_gate → fix before E6/E7
- T-E10-008: New ERRORs in 48h window → identify and fix before Phase F cutover

---

## Quick Reference: Key Commands

```bash
# V11 auth check
python3 -c "
import xmlrpc.client, os
url=os.getenv('ODOO_URL','https://v11.advanced.aero')
db=os.getenv('ODOO_DB','advancedaero')
uid=xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(
  db, os.getenv('ODOO_USER'), os.getenv('ODOO_PASSWORD'), {})
print('UID:', uid)
"

# Run backtest
cd ~/.hermes && python3 scripts/backtest_pricing.py --engine fmv --limit 50

# Check all callback patterns registered
grep -n "ilsq_\|aexq_\|emailq_\|emailgate_" ~/.hermes/hermes-agent/gateway/platforms/telegram.py

# Check all poll functions in cron ticker
grep -n "_run_ils_poll\|_run_aex_poll\|_run_email_rfq_poll\|_run_win_check\|_run_payment_check" \
  ~/.hermes/hermes-agent/gateway/run.py

# 24h error check
grep "ERROR\|CRITICAL" ~/.hermes/logs/gateway.log | grep "$(date +%Y-%m-%d)"

# Quick smoke: import all new services
python3 -c "
import services.v11_client
import services.pricing_intelligence
import services.email_gate
import services.aex_auto_quote
import services.email_rfq_scanner
import services.quote_delivery
import services.auto_send
import services.win_alerts
import services.payment_alerts
print('All imports OK')
"
```
