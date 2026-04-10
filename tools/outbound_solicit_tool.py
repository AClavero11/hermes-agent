"""Outbound solicit draft builder for NON-Summit vendors.

Builds price-free solicit email drafts for historical non-Summit vendors. This
module constructs drafts only — it does not send them. Telegram approval and
SMTP wiring are handled downstream.

Hard invariant (AC directive 2026-04-09): Summit contacts (``@summitmro.com``)
are excluded from the default vendor selection path. The build function
refuses to construct a draft addressed to a Summit recipient and raises
``ValueError`` if asked to. Manual override flows must call
:func:`check_manual_recipient_override` first to force an explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


SUMMIT_DOMAIN = "summitmro.com"
SUBJECT_MAX_LEN = 80
SIGNATURE = "\n\nAnthony Clavero\nPresident"


class SolicitOutcome(str, Enum):
    DRAFTED = "drafted"
    SKIPPED_SUMMIT = "skipped_summit"
    NO_VENDOR = "no_vendor"


@dataclass
class SolicitRequest:
    pn_list: List[str]
    customer_name: Optional[str] = None
    aircraft: Optional[str] = None


@dataclass
class HistoricalVendor:
    email: str
    last_contact_date: str
    pn_context: Optional[str] = None


@dataclass
class SolicitDraft:
    recipient: str
    subject: str
    body: str
    outcome: SolicitOutcome
    warnings: List[str] = field(default_factory=list)


def _is_summit_email(email: str) -> bool:
    """Case-insensitive check whether an email address is on the Summit domain."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    return domain == SUMMIT_DOMAIN


def select_non_summit_vendor(
    candidates: List[HistoricalVendor],
) -> Optional[HistoricalVendor]:
    """Return the first non-Summit vendor from ``candidates``.

    Caller controls sort order; this function only filters. Returns ``None``
    if the list is empty or every candidate is on ``@summitmro.com``.
    """
    for vendor in candidates:
        if not _is_summit_email(vendor.email):
            return vendor
    return None


def verify_price_free(body: str) -> Tuple[bool, List[str]]:
    """Audit a solicit body for price leakage.

    Returns ``(is_clean, offenses)``. Clean means:
    - No ``$`` character anywhere.
    - No ``cost``/``price`` occurrence outside a question form. A line counts
      as a question if it ends with ``?``, starts with ``what``, or contains
      ``how much``.
    """
    offenses: List[str] = []

    if "$" in body:
        offenses.append("contains dollar sign")

    for raw_line in body.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if "cost" not in lowered and "price" not in lowered:
            continue
        is_question = (
            line.endswith("?")
            or lowered.startswith("what")
            or "how much" in lowered
        )
        if not is_question:
            offenses.append(f"non-question cost/price mention: {line!r}")

    return (len(offenses) == 0, offenses)


def _format_subject(pn_list: List[str]) -> str:
    raw = "Need pricing on " + ", ".join(pn_list)
    if len(raw) <= SUBJECT_MAX_LEN:
        return raw
    return raw[: SUBJECT_MAX_LEN - 3] + "..."


def _format_body(request: SolicitRequest) -> str:
    lines: List[str] = []
    header_bits = ["hey, need pricing on the following:"]
    if request.customer_name:
        header_bits.append(f"customer: {request.customer_name}")
    if request.aircraft:
        header_bits.append(f"a/c: {request.aircraft}")
    lines.append(" — ".join(header_bits))
    lines.append("")

    for pn in request.pn_list:
        lines.append(f"{pn} — lmk what you've got")

    lines.append("")
    lines.append("lmk what you can do")
    body = "\n".join(lines) + SIGNATURE
    return body


def build_solicit_draft(
    request: SolicitRequest,
    vendor: HistoricalVendor,
) -> SolicitDraft:
    """Assemble a price-free solicit draft for a non-Summit vendor.

    Raises ``ValueError`` if ``vendor.email`` is on ``@summitmro.com`` or if
    the generated body fails :func:`verify_price_free`.
    """
    if _is_summit_email(vendor.email):
        raise ValueError(
            f"refusing to draft solicit for Summit recipient: {vendor.email}"
        )
    if not request.pn_list:
        raise ValueError("SolicitRequest.pn_list must not be empty")

    subject = _format_subject(request.pn_list)
    body = _format_body(request)

    is_clean, offenses = verify_price_free(body)
    if not is_clean:
        raise ValueError(
            "generated solicit body failed price-free check: " + "; ".join(offenses)
        )

    return SolicitDraft(
        recipient=vendor.email,
        subject=subject,
        body=body,
        outcome=SolicitOutcome.DRAFTED,
        warnings=[],
    )


def check_manual_recipient_override(
    manual_email: str,
) -> Tuple[bool, Optional[str]]:
    """Inspect a manually-typed recipient for Summit exclusion.

    Returns ``(requires_confirmation, warning_message)``. The flag is ``True``
    only when the domain is ``@summitmro.com`` — the Telegram Edit Recipient
    flow must surface the warning and force an explicit confirm before
    sending to Summit.
    """
    if not manual_email or "@" not in manual_email:
        return (False, None)
    if _is_summit_email(manual_email):
        return (
            True,
            "Summit is excluded from auto-solicits. Send anyway?",
        )
    return (False, None)
