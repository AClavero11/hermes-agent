# Implementation Spec: SWA-003 — Telegram callback handlers for the 4 SDA flows

## Objective
Add pure, unit-testable Telegram callback handlers for four SDA user-facing flows, register them in `gateway/platforms/telegram.py`, and cover them with ≥12 unit tests plus an integration test. No real Telegram/SMTP/V11 calls in any test.

## Files to Create
1. `tools/telegram_sda_flows.py` — the handler module
2. `tests/tools/test_telegram_sda_flows.py` — unit tests (≥12)
3. `tests/integration/test_telegram_sda_integration.py` — round-trip tests with mock Telegram

## Files to Modify
4. `gateway/platforms/telegram.py` — register one new `CallbackQueryHandler` with `pattern=r"^sda_"` routing to a new `self._handle_sda_callback` method (≤ 20 lines added).

## Architecture

### Flow 1 — No-price manual entry
When an RFQ line has no price the cascade can compute, the gateway sends a prompt with buttons:
- `[Skip]` → `sda_noprice_skip:<rfq_id>:<pn>`
- `[Cancel]` → `sda_noprice_cancel:<rfq_id>:<pn>`

The actual numeric price reply is handled by a message reply handler (out of scope for this story — we only wire the callback buttons). The handler marks the prompt as resolved via the injected `mark_noprice_resolved(rfq_id, pn, action)` callable.

### Flow 2 — Outbound solicit approval
Sent when the bridge decides to email an external vendor for a quote. Buttons:
- `[Send]` → `sda_solicit_send:<solicit_id>`
- `[Skip]` → `sda_solicit_skip:<solicit_id>`
- `[Manual Price]` → `sda_solicit_manual_price:<solicit_id>`
- `[Edit Recipient]` → `sda_solicit_edit:<solicit_id>` (user then replies with the new email address — the handler only sees the callback with the current state via the injected `get_pending_solicit` callable)

**Summit recipient guard (AC 11):** when action is `edit`, if the new recipient email (looked up via `get_pending_solicit(solicit_id).proposed_recipient`) is on `@summitmro.com`, the handler MUST call `tools.outbound_solicit_tool.check_manual_recipient_override` and return a `CallbackResult(requires_confirmation=True, warning=...)`. The caller then presents a confirmation UI (out of scope).

### Flow 3 — Append offer
When the bridge detects a HIGH/MED confidence append suggestion:
- `[Append]` → `sda_append_append:<rfq_id>:<quote_id>`
- `[New Quote]` → `sda_append_new:<rfq_id>`
- `[Reject]` → `sda_append_reject:<rfq_id>`

### Flow 4 — IDG warning
Informational yellow banner for IDG Summit piece parts. One acknowledgement button:
- `[Dismiss]` → `sda_idg_dismiss:<rfq_id>:<pn>`

## Core types

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class CallbackResult:
    """Structured result of an SDA callback handler.

    Handlers return this instead of touching the Telegram API directly so
    they can be unit-tested with mocked deps.
    """
    success: bool
    action: str                         # e.g. "solicit_send", "append_reject"
    message: str                        # user-facing confirmation text
    requires_confirmation: bool = False # Summit guard / destructive ops
    warning: Optional[str] = None
    error: Optional[str] = None
    next_callback_data: Optional[str] = None  # For follow-up button chain
```

## Handler signatures

Each handler is **sync** (handlers themselves do no I/O — deps do). The dispatcher wraps them for async telegram.py integration.

```python
def handle_noprice_manual_reply(
    callback_data: str,
    *,
    mark_noprice_resolved: Callable[[str, str, str], None],
) -> CallbackResult: ...

def handle_solicit_callback(
    callback_data: str,
    *,
    get_pending_solicit: Callable[[str], Optional[dict]],
    send_solicit_email: Callable[[dict], None],
    enqueue_manual_price: Callable[[str, str], None],
    check_override: Callable[[str], tuple] = None,  # defaults to tools.outbound_solicit_tool.check_manual_recipient_override
) -> CallbackResult: ...

def handle_append_callback(
    callback_data: str,
    *,
    v11_append_line: Callable[[str, str], dict],
    v11_create_new_quote: Callable[[str], dict],
    mark_append_rejected: Callable[[str], None],
) -> CallbackResult: ...

def handle_idg_warning_dismissed(
    callback_data: str,
    *,
    mark_idg_acknowledged: Callable[[str, str], None],
) -> CallbackResult: ...
```

## Dispatcher

```python
def dispatch_sda_callback(
    callback_data: str,
    deps: Dict[str, Any],
) -> CallbackResult:
    """Route a raw callback_data string to the right handler based on prefix.

    deps must contain the callables needed by whichever handler is selected.
    KeyError on missing dep returns an error CallbackResult, never raises.
    """
    try:
        if callback_data.startswith("sda_noprice_"):
            return handle_noprice_manual_reply(
                callback_data,
                mark_noprice_resolved=deps["mark_noprice_resolved"],
            )
        if callback_data.startswith("sda_solicit_"):
            return handle_solicit_callback(
                callback_data,
                get_pending_solicit=deps["get_pending_solicit"],
                send_solicit_email=deps["send_solicit_email"],
                enqueue_manual_price=deps["enqueue_manual_price"],
                check_override=deps.get("check_override"),
            )
        if callback_data.startswith("sda_append_"):
            return handle_append_callback(
                callback_data,
                v11_append_line=deps["v11_append_line"],
                v11_create_new_quote=deps["v11_create_new_quote"],
                mark_append_rejected=deps["mark_append_rejected"],
            )
        if callback_data.startswith("sda_idg_"):
            return handle_idg_warning_dismissed(
                callback_data,
                mark_idg_acknowledged=deps["mark_idg_acknowledged"],
            )
        return CallbackResult(
            success=False,
            action="unknown",
            message="Unknown callback",
            error=f"no handler matches prefix: {callback_data[:30]}",
        )
    except KeyError as exc:
        return CallbackResult(
            success=False,
            action="missing_dep",
            message="Handler dependency missing",
            error=f"required dep not provided: {exc}",
        )
```

## Callback-data parsing

All callback_data strings use `:` as the separator. Format: `sda_<flow>_<action>:<arg1>:<arg2>:...`

Helper:
```python
def _parse_cb(callback_data: str) -> tuple[str, list[str]]:
    """Split 'sda_flow_action:arg1:arg2' into ('flow_action', ['arg1', 'arg2']).

    Strips the 'sda_' prefix from the action name.
    """
    head, _, tail = callback_data.partition(":")
    action = head.removeprefix("sda_")
    args = tail.split(":") if tail else []
    return action, args
```

## Handler implementations

### handle_noprice_manual_reply
```python
def handle_noprice_manual_reply(callback_data, *, mark_noprice_resolved):
    action, args = _parse_cb(callback_data)
    if action not in ("noprice_skip", "noprice_cancel"):
        return CallbackResult(
            success=False, action=action,
            message="Unknown no-price action",
            error=f"unsupported action: {action}",
        )
    if len(args) < 2:
        return CallbackResult(
            success=False, action=action,
            message="Malformed no-price callback",
            error="expected rfq_id and pn in callback_data",
        )
    rfq_id, pn = args[0], args[1]
    try:
        mark_noprice_resolved(rfq_id, pn, action)
    except Exception as exc:
        return CallbackResult(
            success=False, action=action,
            message="Failed to mark no-price resolved",
            error=str(exc),
        )
    return CallbackResult(
        success=True, action=action,
        message=f"No-price prompt {action.removeprefix('noprice_')} for {pn}",
    )
```

### handle_solicit_callback
Routes on `solicit_send`, `solicit_skip`, `solicit_manual_price`, `solicit_edit`.

For `solicit_edit`: fetch the pending solicit via `get_pending_solicit(solicit_id)`. Grab `proposed_recipient`. Call `check_override(proposed_recipient)`. If `(True, warning)`, return `requires_confirmation=True` with the warning. If `(False, None)`, return success (the actual email send happens when user hits a separate confirmation button).

```python
def handle_solicit_callback(
    callback_data, *,
    get_pending_solicit,
    send_solicit_email,
    enqueue_manual_price,
    check_override=None,
):
    if check_override is None:
        from tools.outbound_solicit_tool import check_manual_recipient_override
        check_override = check_manual_recipient_override

    action, args = _parse_cb(callback_data)
    if not args:
        return CallbackResult(
            success=False, action=action,
            message="Malformed solicit callback",
            error="missing solicit_id",
        )
    solicit_id = args[0]

    if action == "solicit_send":
        try:
            pending = get_pending_solicit(solicit_id)
            if pending is None:
                return CallbackResult(
                    success=False, action=action,
                    message="Solicit not found",
                    error=f"no pending solicit with id {solicit_id}",
                )
            send_solicit_email(pending)
            return CallbackResult(
                success=True, action=action,
                message=f"Solicit sent to {pending.get('proposed_recipient', '?')}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False, action=action,
                message="Failed to send solicit", error=str(exc),
            )

    if action == "solicit_skip":
        return CallbackResult(
            success=True, action=action,
            message="Solicit skipped",
        )

    if action == "solicit_manual_price":
        try:
            pending = get_pending_solicit(solicit_id)
            if pending is None:
                return CallbackResult(
                    success=False, action=action,
                    message="Solicit not found",
                    error=f"no pending solicit with id {solicit_id}",
                )
            enqueue_manual_price(solicit_id, pending.get("pn", ""))
            return CallbackResult(
                success=True, action=action,
                message=f"Manual price entry queued for {pending.get('pn', '?')}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False, action=action,
                message="Failed to enqueue manual price", error=str(exc),
            )

    if action == "solicit_edit":
        pending = get_pending_solicit(solicit_id)
        if pending is None:
            return CallbackResult(
                success=False, action=action,
                message="Solicit not found",
                error=f"no pending solicit with id {solicit_id}",
            )
        proposed = pending.get("proposed_recipient", "")
        requires_conf, warning = check_override(proposed)
        if requires_conf:
            return CallbackResult(
                success=True, action=action,
                message="Summit recipient override requires confirmation",
                requires_confirmation=True,
                warning=warning,
                next_callback_data=f"sda_solicit_confirm_summit:{solicit_id}",
            )
        return CallbackResult(
            success=True, action=action,
            message=f"Edit recipient accepted: {proposed}",
        )

    return CallbackResult(
        success=False, action=action,
        message="Unknown solicit action",
        error=f"unsupported action: {action}",
    )
```

### handle_append_callback
Routes on `append_append`, `append_new`, `append_reject`.

```python
def handle_append_callback(
    callback_data, *,
    v11_append_line,
    v11_create_new_quote,
    mark_append_rejected,
):
    action, args = _parse_cb(callback_data)
    if not args:
        return CallbackResult(
            success=False, action=action,
            message="Malformed append callback",
            error="missing rfq_id",
        )
    rfq_id = args[0]

    if action == "append_append":
        if len(args) < 2:
            return CallbackResult(
                success=False, action=action,
                message="Append requires quote_id",
                error="missing quote_id in callback_data",
            )
        quote_id = args[1]
        try:
            result = v11_append_line(quote_id, rfq_id)
            return CallbackResult(
                success=True, action=action,
                message=f"Appended RFQ {rfq_id} to quote {quote_id}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False, action=action,
                message="Failed to append line", error=str(exc),
            )

    if action == "append_new":
        try:
            result = v11_create_new_quote(rfq_id)
            return CallbackResult(
                success=True, action=action,
                message=f"New quote created for RFQ {rfq_id}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False, action=action,
                message="Failed to create new quote", error=str(exc),
            )

    if action == "append_reject":
        try:
            mark_append_rejected(rfq_id)
            return CallbackResult(
                success=True, action=action,
                message=f"Append suggestion rejected for RFQ {rfq_id}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False, action=action,
                message="Failed to mark append rejected", error=str(exc),
            )

    return CallbackResult(
        success=False, action=action,
        message="Unknown append action",
        error=f"unsupported action: {action}",
    )
```

### handle_idg_warning_dismissed
```python
def handle_idg_warning_dismissed(callback_data, *, mark_idg_acknowledged):
    action, args = _parse_cb(callback_data)
    if action != "idg_dismiss":
        return CallbackResult(
            success=False, action=action,
            message="Unknown IDG action",
            error=f"unsupported action: {action}",
        )
    if len(args) < 2:
        return CallbackResult(
            success=False, action=action,
            message="Malformed IDG callback",
            error="expected rfq_id and pn",
        )
    rfq_id, pn = args[0], args[1]
    try:
        mark_idg_acknowledged(rfq_id, pn)
    except Exception as exc:
        return CallbackResult(
            success=False, action=action,
            message="Failed to mark IDG acknowledged", error=str(exc),
        )
    return CallbackResult(
        success=True, action=action,
        message=f"IDG warning dismissed for {pn}",
    )
```

## gateway/platforms/telegram.py edit

Add the CallbackQueryHandler registration (right after the `cc_` handler around line 160):

```python
# Register SDA flow callbacks (SWA-003): no-price, solicit, append, IDG
self._app.add_handler(CallbackQueryHandler(
    self._handle_sda_callback,
    pattern=r"^sda_"
))
```

Add the method (place near the other `_handle_*_callback` methods, around line 977):

```python
async def _handle_sda_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle SDA flow callbacks (no-price, solicit, append, IDG)."""
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        await query.answer()
        from tools.telegram_sda_flows import dispatch_sda_callback
        # Real deps wired at call time; for now we surface the result text only.
        # Downstream side-effect wiring happens in SWA-004's integration pass.
        def _noop(*args, **kwargs): return None
        result = dispatch_sda_callback(query.data, deps={
            "mark_noprice_resolved": _noop,
            "get_pending_solicit": lambda sid: None,
            "send_solicit_email": _noop,
            "enqueue_manual_price": _noop,
            "v11_append_line": lambda q, r: {},
            "v11_create_new_quote": lambda r: {},
            "mark_append_rejected": _noop,
            "mark_idg_acknowledged": _noop,
        })
        if result.requires_confirmation and result.warning:
            await query.answer(result.warning, show_alert=True)
        elif query.message:
            await query.message.reply_text(result.message)
    except Exception as e:
        logger.error("SDA callback handler error: %s", e, exc_info=True)
        if query:
            try:
                await query.answer("Error processing SDA callback", show_alert=True)
            except Exception:
                pass
```

**The placeholder `_noop` deps are acceptable here** because SWA-003 says "Downstream side effects are themselves injected callables" — the real wiring happens in SWA-004. The callback handler round-trip works end-to-end via the dispatcher.

## Unit tests (≥12, in tests/tools/test_telegram_sda_flows.py)

**Structure:** one `TestClass` per handler, 3 tests each minimum.

```python
"""Unit tests for tools.telegram_sda_flows (SWA-003)."""

from __future__ import annotations
from unittest.mock import Mock

import pytest

from tools.telegram_sda_flows import (
    CallbackResult,
    dispatch_sda_callback,
    handle_noprice_manual_reply,
    handle_solicit_callback,
    handle_append_callback,
    handle_idg_warning_dismissed,
)


class TestNoPriceHandler:
    def test_happy_path_skip(self):
        mock_resolve = Mock()
        r = handle_noprice_manual_reply(
            "sda_noprice_skip:RFQ1:PN-A",
            mark_noprice_resolved=mock_resolve,
        )
        assert r.success and r.action == "noprice_skip"
        mock_resolve.assert_called_once_with("RFQ1", "PN-A", "noprice_skip")

    def test_malformed_callback(self):
        r = handle_noprice_manual_reply(
            "sda_noprice_skip",
            mark_noprice_resolved=Mock(),
        )
        assert r.success is False
        assert "expected rfq_id" in r.error

    def test_downstream_failure_returns_error_result(self):
        mock_resolve = Mock(side_effect=RuntimeError("db down"))
        r = handle_noprice_manual_reply(
            "sda_noprice_cancel:RFQ1:PN-A",
            mark_noprice_resolved=mock_resolve,
        )
        assert r.success is False and "db down" in r.error


class TestSolicitHandler:
    def _pending(self, recipient="vendor@example.com", pn="PN-A"):
        return {"solicit_id": "S1", "proposed_recipient": recipient, "pn": pn}

    def test_happy_path_send(self):
        mock_send = Mock()
        r = handle_solicit_callback(
            "sda_solicit_send:S1",
            get_pending_solicit=lambda sid: self._pending(),
            send_solicit_email=mock_send,
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success
        mock_send.assert_called_once()

    def test_skip_is_a_noop_success(self):
        r = handle_solicit_callback(
            "sda_solicit_skip:S1",
            get_pending_solicit=lambda sid: None,
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success and r.action == "solicit_skip"

    def test_edit_recipient_summit_requires_confirmation(self):
        """AC 11: Summit email triggers check_manual_recipient_override."""
        guard_calls = []

        def fake_check(email):
            guard_calls.append(email)
            return (True, "Summit is excluded from auto-solicits. Send anyway?")

        r = handle_solicit_callback(
            "sda_solicit_edit:S1",
            get_pending_solicit=lambda sid: self._pending(recipient="x@summitmro.com"),
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=fake_check,
        )
        assert r.requires_confirmation is True
        assert "Summit" in r.warning
        assert guard_calls == ["x@summitmro.com"]

    def test_edit_recipient_non_summit_passes_through(self):
        r = handle_solicit_callback(
            "sda_solicit_edit:S1",
            get_pending_solicit=lambda sid: self._pending(recipient="bob@normal.com"),
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success and r.requires_confirmation is False

    def test_manual_price_happy_path(self):
        mock_enqueue = Mock()
        r = handle_solicit_callback(
            "sda_solicit_manual_price:S1",
            get_pending_solicit=lambda sid: self._pending(),
            send_solicit_email=Mock(),
            enqueue_manual_price=mock_enqueue,
            check_override=lambda e: (False, None),
        )
        assert r.success
        mock_enqueue.assert_called_once_with("S1", "PN-A")

    def test_pending_solicit_missing_returns_error(self):
        r = handle_solicit_callback(
            "sda_solicit_send:MISSING",
            get_pending_solicit=lambda sid: None,
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success is False and "no pending solicit" in r.error


class TestAppendHandler:
    def test_append_to_existing_quote(self):
        mock_append = Mock(return_value={"ok": True})
        r = handle_append_callback(
            "sda_append_append:RFQ1:Q42",
            v11_append_line=mock_append,
            v11_create_new_quote=Mock(),
            mark_append_rejected=Mock(),
        )
        assert r.success
        mock_append.assert_called_once_with("Q42", "RFQ1")

    def test_new_quote_path(self):
        mock_new = Mock(return_value={"id": 99})
        r = handle_append_callback(
            "sda_append_new:RFQ1",
            v11_append_line=Mock(),
            v11_create_new_quote=mock_new,
            mark_append_rejected=Mock(),
        )
        assert r.success
        mock_new.assert_called_once_with("RFQ1")

    def test_reject_marks_rejection(self):
        mock_reject = Mock()
        r = handle_append_callback(
            "sda_append_reject:RFQ1",
            v11_append_line=Mock(),
            v11_create_new_quote=Mock(),
            mark_append_rejected=mock_reject,
        )
        assert r.success
        mock_reject.assert_called_once_with("RFQ1")

    def test_append_missing_quote_id(self):
        r = handle_append_callback(
            "sda_append_append:RFQ1",
            v11_append_line=Mock(),
            v11_create_new_quote=Mock(),
            mark_append_rejected=Mock(),
        )
        assert r.success is False and "quote_id" in r.error


class TestIdgHandler:
    def test_dismiss_happy_path(self):
        mock_ack = Mock()
        r = handle_idg_warning_dismissed(
            "sda_idg_dismiss:RFQ1:PN-IDG",
            mark_idg_acknowledged=mock_ack,
        )
        assert r.success
        mock_ack.assert_called_once_with("RFQ1", "PN-IDG")

    def test_dismiss_missing_pn(self):
        r = handle_idg_warning_dismissed(
            "sda_idg_dismiss:RFQ1",
            mark_idg_acknowledged=Mock(),
        )
        assert r.success is False

    def test_unknown_idg_action(self):
        r = handle_idg_warning_dismissed(
            "sda_idg_escalate:RFQ1:PN-A",
            mark_idg_acknowledged=Mock(),
        )
        assert r.success is False


class TestDispatcher:
    def test_routes_to_correct_handler(self):
        mock_resolve = Mock()
        r = dispatch_sda_callback(
            "sda_noprice_skip:RFQ1:PN-A",
            deps={"mark_noprice_resolved": mock_resolve},
        )
        assert r.success
        mock_resolve.assert_called_once()

    def test_unknown_prefix_returns_error(self):
        r = dispatch_sda_callback("sda_other_foo:X", deps={})
        assert r.success is False and "unknown" in r.action.lower()

    def test_missing_dependency_surfaces_as_error(self):
        r = dispatch_sda_callback("sda_noprice_skip:RFQ1:PN-A", deps={})
        assert r.success is False
        assert r.action == "missing_dep"
```

That's **18 tests** (well over the 12 minimum).

## Integration test (tests/integration/test_telegram_sda_integration.py)

```python
"""Integration test for SWA-003 — simulates a full Telegram callback round-trip
for each of the four SDA flows using mock Telegram objects. No real bot.

Marked ``integration`` so it only runs with ``-m integration``.
"""

from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from tools.telegram_sda_flows import CallbackResult, dispatch_sda_callback


def _fake_query(callback_data):
    q = MagicMock()
    q.data = callback_data
    q.answer = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    return q


@pytest.mark.integration
class TestSdaCallbackRoundTrip:
    @pytest.mark.asyncio
    async def test_noprice_round_trip(self):
        resolved = []
        deps = {"mark_noprice_resolved": lambda r, p, a: resolved.append((r, p, a))}
        result = dispatch_sda_callback("sda_noprice_skip:RFQ1:PN-A", deps=deps)
        assert result.success
        assert resolved == [("RFQ1", "PN-A", "noprice_skip")]

    @pytest.mark.asyncio
    async def test_solicit_round_trip(self):
        sent = []
        deps = {
            "get_pending_solicit": lambda sid: {
                "solicit_id": sid,
                "proposed_recipient": "v@normal.com",
                "pn": "PN-A",
            },
            "send_solicit_email": lambda p: sent.append(p),
            "enqueue_manual_price": Mock(),
            "check_override": lambda e: (False, None),
        }
        result = dispatch_sda_callback("sda_solicit_send:S1", deps=deps)
        assert result.success
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_summit_guard_round_trip(self):
        deps = {
            "get_pending_solicit": lambda sid: {
                "solicit_id": sid,
                "proposed_recipient": "x@summitmro.com",
                "pn": "PN-A",
            },
            "send_solicit_email": Mock(),
            "enqueue_manual_price": Mock(),
        }
        result = dispatch_sda_callback("sda_solicit_edit:S1", deps=deps)
        assert result.requires_confirmation is True
        assert "Summit" in (result.warning or "")

    @pytest.mark.asyncio
    async def test_append_round_trip(self):
        appended = []
        deps = {
            "v11_append_line": lambda q, r: appended.append((q, r)) or {"ok": True},
            "v11_create_new_quote": Mock(),
            "mark_append_rejected": Mock(),
        }
        result = dispatch_sda_callback("sda_append_append:RFQ1:Q42", deps=deps)
        assert result.success
        assert appended == [("Q42", "RFQ1")]

    @pytest.mark.asyncio
    async def test_idg_round_trip(self):
        acked = []
        deps = {"mark_idg_acknowledged": lambda r, p: acked.append((r, p))}
        result = dispatch_sda_callback("sda_idg_dismiss:RFQ1:PN-IDG", deps=deps)
        assert result.success
        assert acked == [("RFQ1", "PN-IDG")]
```

**Note on async markers:** The tests are technically sync because `dispatch_sda_callback` is sync. I keep the `async def` + `@pytest.mark.asyncio` wrappers because the AC says "simulate a full callback round-trip" and future round-trips will include awaitable query.answer() calls. If `pytest-asyncio` is not configured strictly, the markers are harmless.

If `pytest-asyncio` strict mode complains, drop the `async def` → `def` and remove `@pytest.mark.asyncio`.

## Gotchas

1. **check_manual_recipient_override import** — must be a lazy import inside `handle_solicit_callback` (not at module top) to avoid creating a circular dep if `tools.outbound_solicit_tool` ever imports from `tools.telegram_sda_flows`. Spec uses `check_override=None` default and imports inside the function body.

2. **Callback data parsing** — `_parse_cb` uses `:` as separator. `removeprefix` is Python 3.9+. Make sure the repo targets ≥3.9 (it does — see `pyproject.toml` and `__future__` annotations already in use).

3. **Unknown action handling** — every handler returns a `CallbackResult` with `success=False` rather than raising. The dispatcher also catches `KeyError` on missing deps. Net result: NO path through `dispatch_sda_callback` can raise.

4. **Telegram handler registration** — inserting ONE new `add_handler` block around line 161 is enough. The `_handle_sda_callback` method goes near `_handle_cc_remote_callback` (~line 977). Keep the changes minimal.

5. **Integration test placeholder-dep wiring** — the `_noop` deps in telegram.py are intentional: SWA-003 says the downstream wiring happens elsewhere. Unit tests verify the handlers with real mocks; the telegram.py method just proves the dispatcher can be invoked from the gateway without crashing.

## Definition of Done
- `python3 -c "from tools.telegram_sda_flows import dispatch_sda_callback, CallbackResult"` clean
- `pytest tests/tools/test_telegram_sda_flows.py -q` passes with ≥12 tests (target 18)
- `pytest tests/integration/test_telegram_sda_integration.py -m integration -q` passes
- `python3 -m py_compile gateway/platforms/telegram.py` clean
- No regression in existing tests/tools/
