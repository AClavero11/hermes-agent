"""Telegram callback handlers for the four SDA user-facing flows (SWA-003).

Pure, unit-testable handlers. Each handler takes a raw callback_data string and
a set of injected callable dependencies, returning a structured CallbackResult.
No Telegram, V11, or SMTP I/O happens inside this module — side effects are
performed exclusively through the injected callables so the handlers can be
tested with simple mocks.

The four flows:
    1. No-price manual entry (sda_noprice_*)
    2. Outbound solicit approval (sda_solicit_*)
    3. Append offer (sda_append_*)
    4. IDG Summit piece-part warning (sda_idg_*)

The dispatcher ``dispatch_sda_callback`` routes by callback_data prefix and
never raises — missing dependencies or unknown prefixes return an error-shaped
``CallbackResult``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class CallbackResult:
    """Structured result of an SDA callback handler.

    Handlers return this instead of touching the Telegram API directly so
    they can be unit-tested with mocked deps.
    """

    success: bool
    action: str                         # e.g. "solicit_send", "append_reject"
    message: str                        # user-facing confirmation text
    requires_confirmation: bool = False  # Summit guard / destructive ops
    warning: Optional[str] = None
    error: Optional[str] = None
    next_callback_data: Optional[str] = None  # For follow-up button chain


def _parse_cb(callback_data: str) -> Tuple[str, List[str]]:
    """Split ``sda_flow_action:arg1:arg2`` into ``('flow_action', ['arg1', 'arg2'])``.

    Strips the ``sda_`` prefix from the action name.
    """
    head, _, tail = callback_data.partition(":")
    action = head.removeprefix("sda_")
    args = tail.split(":") if tail else []
    return action, args


# ---------------------------------------------------------------------------
# Flow 1 — No-price manual entry
# ---------------------------------------------------------------------------

def handle_noprice_manual_reply(
    callback_data: str,
    *,
    mark_noprice_resolved: Callable[[str, str, str], None],
) -> CallbackResult:
    """Handle the Skip/Cancel buttons on a no-price prompt.

    The actual numeric price reply is handled by a separate message reply
    handler (out of scope for SWA-003). This handler only processes the
    two button callbacks.
    """
    action, args = _parse_cb(callback_data)
    if action not in ("noprice_skip", "noprice_cancel"):
        return CallbackResult(
            success=False,
            action=action,
            message="Unknown no-price action",
            error=f"unsupported action: {action}",
        )
    if len(args) < 2:
        return CallbackResult(
            success=False,
            action=action,
            message="Malformed no-price callback",
            error="expected rfq_id and pn in callback_data",
        )
    rfq_id, pn = args[0], args[1]
    try:
        mark_noprice_resolved(rfq_id, pn, action)
    except Exception as exc:
        return CallbackResult(
            success=False,
            action=action,
            message="Failed to mark no-price resolved",
            error=str(exc),
        )
    return CallbackResult(
        success=True,
        action=action,
        message=f"No-price prompt {action.removeprefix('noprice_')} for {pn}",
    )


# ---------------------------------------------------------------------------
# Flow 2 — Outbound solicit approval
# ---------------------------------------------------------------------------

def handle_solicit_callback(
    callback_data: str,
    *,
    get_pending_solicit: Callable[[str], Optional[dict]],
    send_solicit_email: Callable[[dict], None],
    enqueue_manual_price: Callable[[str, str], None],
    check_override: Optional[Callable[[str], Tuple[bool, Optional[str]]]] = None,
) -> CallbackResult:
    """Handle the Send / Skip / Manual Price / Edit Recipient buttons.

    AC 11 (Summit recipient guard): when the action is ``solicit_edit`` and
    the pending solicit's ``proposed_recipient`` is a Summit address, the
    handler must call ``check_manual_recipient_override`` and return a
    result with ``requires_confirmation=True`` plus the warning.
    """
    if check_override is None:
        # Lazy import to avoid circular dependency with tools.outbound_solicit_tool
        from tools.outbound_solicit_tool import check_manual_recipient_override
        check_override = check_manual_recipient_override

    action, args = _parse_cb(callback_data)
    if not args:
        return CallbackResult(
            success=False,
            action=action,
            message="Malformed solicit callback",
            error="missing solicit_id",
        )
    solicit_id = args[0]

    if action == "solicit_send":
        try:
            pending = get_pending_solicit(solicit_id)
            if pending is None:
                return CallbackResult(
                    success=False,
                    action=action,
                    message="Solicit not found",
                    error=f"no pending solicit with id {solicit_id}",
                )
            send_solicit_email(pending)
            return CallbackResult(
                success=True,
                action=action,
                message=f"Solicit sent to {pending.get('proposed_recipient', '?')}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False,
                action=action,
                message="Failed to send solicit",
                error=str(exc),
            )

    if action == "solicit_skip":
        return CallbackResult(
            success=True,
            action=action,
            message="Solicit skipped",
        )

    if action == "solicit_manual_price":
        try:
            pending = get_pending_solicit(solicit_id)
            if pending is None:
                return CallbackResult(
                    success=False,
                    action=action,
                    message="Solicit not found",
                    error=f"no pending solicit with id {solicit_id}",
                )
            enqueue_manual_price(solicit_id, pending.get("pn", ""))
            return CallbackResult(
                success=True,
                action=action,
                message=f"Manual price entry queued for {pending.get('pn', '?')}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False,
                action=action,
                message="Failed to enqueue manual price",
                error=str(exc),
            )

    if action == "solicit_edit":
        pending = get_pending_solicit(solicit_id)
        if pending is None:
            return CallbackResult(
                success=False,
                action=action,
                message="Solicit not found",
                error=f"no pending solicit with id {solicit_id}",
            )
        proposed = pending.get("proposed_recipient", "")
        requires_conf, warning = check_override(proposed)
        if requires_conf:
            return CallbackResult(
                success=True,
                action=action,
                message="Summit recipient override requires confirmation",
                requires_confirmation=True,
                warning=warning,
                next_callback_data=f"sda_solicit_confirm_summit:{solicit_id}",
            )
        return CallbackResult(
            success=True,
            action=action,
            message=f"Edit recipient accepted: {proposed}",
        )

    return CallbackResult(
        success=False,
        action=action,
        message="Unknown solicit action",
        error=f"unsupported action: {action}",
    )


# ---------------------------------------------------------------------------
# Flow 3 — Append offer
# ---------------------------------------------------------------------------

def handle_append_callback(
    callback_data: str,
    *,
    v11_append_line: Callable[[str, str], dict],
    v11_create_new_quote: Callable[[str], dict],
    mark_append_rejected: Callable[[str], None],
) -> CallbackResult:
    """Handle the Append / New Quote / Reject buttons on an append suggestion."""
    action, args = _parse_cb(callback_data)
    if not args:
        return CallbackResult(
            success=False,
            action=action,
            message="Malformed append callback",
            error="missing rfq_id",
        )
    rfq_id = args[0]

    if action == "append_append":
        if len(args) < 2:
            return CallbackResult(
                success=False,
                action=action,
                message="Append requires quote_id",
                error="missing quote_id in callback_data",
            )
        quote_id = args[1]
        try:
            v11_append_line(quote_id, rfq_id)
            return CallbackResult(
                success=True,
                action=action,
                message=f"Appended RFQ {rfq_id} to quote {quote_id}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False,
                action=action,
                message="Failed to append line",
                error=str(exc),
            )

    if action == "append_new":
        try:
            v11_create_new_quote(rfq_id)
            return CallbackResult(
                success=True,
                action=action,
                message=f"New quote created for RFQ {rfq_id}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False,
                action=action,
                message="Failed to create new quote",
                error=str(exc),
            )

    if action == "append_reject":
        try:
            mark_append_rejected(rfq_id)
            return CallbackResult(
                success=True,
                action=action,
                message=f"Append suggestion rejected for RFQ {rfq_id}",
            )
        except Exception as exc:
            return CallbackResult(
                success=False,
                action=action,
                message="Failed to mark append rejected",
                error=str(exc),
            )

    return CallbackResult(
        success=False,
        action=action,
        message="Unknown append action",
        error=f"unsupported action: {action}",
    )


# ---------------------------------------------------------------------------
# Flow 4 — IDG warning
# ---------------------------------------------------------------------------

def handle_idg_warning_dismissed(
    callback_data: str,
    *,
    mark_idg_acknowledged: Callable[[str, str], None],
) -> CallbackResult:
    """Handle the Dismiss button on an IDG Summit piece-part warning banner."""
    action, args = _parse_cb(callback_data)
    if action != "idg_dismiss":
        return CallbackResult(
            success=False,
            action=action,
            message="Unknown IDG action",
            error=f"unsupported action: {action}",
        )
    if len(args) < 2:
        return CallbackResult(
            success=False,
            action=action,
            message="Malformed IDG callback",
            error="expected rfq_id and pn",
        )
    rfq_id, pn = args[0], args[1]
    try:
        mark_idg_acknowledged(rfq_id, pn)
    except Exception as exc:
        return CallbackResult(
            success=False,
            action=action,
            message="Failed to mark IDG acknowledged",
            error=str(exc),
        )
    return CallbackResult(
        success=True,
        action=action,
        message=f"IDG warning dismissed for {pn}",
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_sda_callback(
    callback_data: str,
    deps: Dict[str, Any],
) -> CallbackResult:
    """Route a raw callback_data string to the right handler based on prefix.

    ``deps`` must contain the callables needed by whichever handler is
    selected. A missing dep is surfaced as a ``CallbackResult`` with
    ``action="missing_dep"`` — this function never raises.
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
