"""Verify V11 pricing engine capabilities and generate atlas PRD addendum stubs.

Probes the engine for two capabilities:
    1. Condition-tier compression for SRUs/piece-parts (tight AR/SV spread).
    2. LIFO Advanced ID selection with teardown cost recovery.

If either capability is missing, auto-generates addendum stub JSON files
that AC can hand to the atlas execution track. No modification of the
migration PRD is performed.

This is a CLI script. Probe callables are injected so tests can mock them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, List, Optional


class Capability(str, Enum):
    SRU_LRU_COMPRESSION = "sru_lru_compression"
    LIFO_TEARDOWN = "lifo_teardown"


@dataclass
class CapabilityResult:
    capability: Capability
    compliant: bool
    detail: str
    measured: dict = field(default_factory=dict)


@dataclass
class VerificationReport:
    timestamp: str
    capabilities: List[CapabilityResult]
    missing: List[Capability]
    addendum_files_created: List[str]


# ---------------------------------------------------------------------------
# Capability probes
# ---------------------------------------------------------------------------

def _delta_pct(ar_price: float, sv_price: float) -> float:
    if sv_price <= 0:
        return 0.0
    return (sv_price - ar_price) / sv_price * 100.0


def probe_sru_lru_compression(
    sru_pn: str,
    lru_pn: str,
    sru_probe: Callable[[str, str], Optional[dict]],
    lru_probe: Callable[[str, str], Optional[dict]],
    compression_threshold_pct: float = 12.0,
) -> CapabilityResult:
    """Verify the engine compresses AR/SV spread for SRUs vs LRUs.

    A compliant engine has:
        sru_delta_pct < threshold (tight spread for SRUs/piece-parts)
        lru_delta_pct >= threshold (wide spread for rotables/LRUs)
    """
    sru_ar = sru_probe(sru_pn, "AR")
    sru_sv = sru_probe(sru_pn, "SV")
    if not sru_ar or not sru_sv or "price" not in sru_ar or "price" not in sru_sv:
        return CapabilityResult(
            capability=Capability.SRU_LRU_COMPRESSION,
            compliant=False,
            detail="SRU probe returned no price",
            measured={"threshold": compression_threshold_pct},
        )

    lru_ar = lru_probe(lru_pn, "AR")
    lru_sv = lru_probe(lru_pn, "SV")
    if not lru_ar or not lru_sv or "price" not in lru_ar or "price" not in lru_sv:
        return CapabilityResult(
            capability=Capability.SRU_LRU_COMPRESSION,
            compliant=False,
            detail="LRU probe returned no price",
            measured={"threshold": compression_threshold_pct},
        )

    sru_delta_pct = _delta_pct(sru_ar["price"], sru_sv["price"])
    lru_delta_pct = _delta_pct(lru_ar["price"], lru_sv["price"])

    compliant = (
        sru_delta_pct < compression_threshold_pct
        and lru_delta_pct >= compression_threshold_pct
    )

    if compliant:
        detail = (
            f"SRU spread {sru_delta_pct:.2f}% < {compression_threshold_pct}% "
            f"and LRU spread {lru_delta_pct:.2f}% >= {compression_threshold_pct}%"
        )
    elif sru_delta_pct >= compression_threshold_pct:
        detail = (
            f"SRU spread {sru_delta_pct:.2f}% is too wide "
            f"(>= {compression_threshold_pct}%); engine is not compressing SRUs"
        )
    else:
        detail = (
            f"LRU spread {lru_delta_pct:.2f}% is too tight "
            f"(< {compression_threshold_pct}%); unit_class not plumbed through"
        )

    return CapabilityResult(
        capability=Capability.SRU_LRU_COMPRESSION,
        compliant=compliant,
        detail=detail,
        measured={
            "sru_delta_pct": sru_delta_pct,
            "lru_delta_pct": lru_delta_pct,
            "threshold": compression_threshold_pct,
        },
    )


def probe_lifo_teardown(
    pn: str,
    engine_probe: Callable[[str], Optional[dict]],
) -> CapabilityResult:
    """Verify the engine picks the newest teardown-bearing lot (LIFO)."""
    data = engine_probe(pn)
    if not data or "selected_lot_id" not in data or "lots_available" not in data:
        return CapabilityResult(
            capability=Capability.LIFO_TEARDOWN,
            compliant=False,
            detail="probe returned no data",
            measured={},
        )

    selected = data["selected_lot_id"]
    lots = data["lots_available"] or []

    teardown_lots = [
        lot for lot in lots
        if lot.get("teardown_cost") and lot["teardown_cost"] > 0
    ]

    if len(teardown_lots) < 2:
        return CapabilityResult(
            capability=Capability.LIFO_TEARDOWN,
            compliant=False,
            detail="not enough teardown-bearing lots to test LIFO",
            measured={
                "selected": selected,
                "teardown_lots_considered": len(teardown_lots),
            },
        )

    teardown_lots_sorted = sorted(
        teardown_lots, key=lambda lot: lot["in_date"], reverse=True
    )
    expected = teardown_lots_sorted[0]["lot_id"]

    compliant = selected == expected
    if compliant:
        detail = f"engine picked newest teardown lot {selected} (LIFO)"
    else:
        detail = (
            f"engine picked {selected} but LIFO expected {expected}; "
            f"engine appears to use FIFO default"
        )

    return CapabilityResult(
        capability=Capability.LIFO_TEARDOWN,
        compliant=compliant,
        detail=detail,
        measured={
            "selected": selected,
            "expected": expected,
            "teardown_lots_considered": len(teardown_lots),
        },
    )


# ---------------------------------------------------------------------------
# Addendum stub generation
# ---------------------------------------------------------------------------

_STUB_TEMPLATES = {
    Capability.SRU_LRU_COMPRESSION: {
        "slug": "srulru-compression",
        "description": (
            "V11 pricing engine does not compress the AR/SV condition spread "
            "for SRUs / piece-parts. Rotables and LRUs should retain the wide "
            "spread; SRUs should use a tight spread. Atlas must plumb "
            "unit_class through EnrichedPart and branch CONDITION_MULTIPLIER "
            "logic accordingly."
        ),
        "story_title": "Plumb unit_class into pricing engine and branch CONDITION_MULTIPLIER",
        "story_description": (
            "Add unit_class to the EnrichedPart input used by the pricing "
            "engine, then branch the CONDITION_MULTIPLIER logic so SRUs and "
            "piece-parts get a tight AR/SV spread (configurable, default 4%) "
            "while rotables and LRUs retain the existing wider spread "
            "(default 8%)."
        ),
        "acceptance_criteria": [
            "EnrichedPart exposes unit_class sourced from the V11 product master",
            "Pricing engine branches CONDITION_MULTIPLIER on unit_class with "
            "configurable SRU spread (default 4%) and LRU spread (default 8%)",
            "Unit tests cover SRU, LRU, and unknown unit_class cases including "
            "fallback to the LRU default when unit_class is missing",
        ],
    },
    Capability.LIFO_TEARDOWN: {
        "slug": "lifo-teardown",
        "description": (
            "V11 pricing engine does not perform LIFO selection on lots with "
            "recent teardown cost. When >=2 lots are available and one has "
            "outstanding teardown cost recovery from a higher-assembly "
            "teardown, the newest such lot should be selected first. "
            "Current behavior is FIFO default."
        ),
        "story_title": "Add LIFO lot-ranking mode with teardown cost recovery awareness",
        "story_description": (
            "Extend lot-ranking in the pricing engine with a LIFO sort mode "
            "keyed on teardown_cost_recency_date. Add "
            "teardown_cost_recovery_pending (boolean) and "
            "teardown_cost_recency_date (datetime) fields to the lot record. "
            "When no lot has teardown cost, fall back to the existing FIFO "
            "ranking."
        ),
        "acceptance_criteria": [
            "Lot record exposes teardown_cost_recovery_pending and "
            "teardown_cost_recency_date fields",
            "Lot-ranking supports a LIFO mode that sorts teardown-bearing "
            "lots by teardown_cost_recency_date descending",
            "When no lot has teardown_cost > 0 the engine falls back to FIFO "
            "and existing tests continue to pass",
        ],
    },
}


def generate_addendum_stubs(
    missing: List[Capability],
    output_dir: str = "tasks",
) -> List[str]:
    """Write a prd-atlas-addendum-<slug>.json stub for each missing capability.

    Returns the list of file paths created. If a file already exists, it is
    not overwritten and the returned entry is suffixed with
    '(existed, skipped)'.
    """
    created: List[str] = []
    os.makedirs(output_dir, exist_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat()

    for capability in missing:
        template = _STUB_TEMPLATES[capability]
        slug = template["slug"]
        path = os.path.join(output_dir, f"prd-atlas-addendum-{slug}.json")

        if os.path.exists(path):
            created.append(f"{path} (existed, skipped)")
            continue

        payload = {
            "project": f"atlas-addendum-{slug}",
            "branchName": f"ralph/atlas-addendum-{slug}",
            "description": template["description"],
            "source": "auto-generated by scripts/verify_engine_capabilities.py",
            "generated_at": generated_at,
            "userStories": [
                {
                    "id": "AA-001",
                    "title": template["story_title"],
                    "description": template["story_description"],
                    "acceptanceCriteria": list(template["acceptance_criteria"]),
                    "priority": "P1",
                    "estimated_hours": 4,
                    "passes": False,
                    "notes": (
                        "Auto-generated stub. Atlas team to fill in "
                        "implementation details."
                    ),
                }
            ],
        }

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

        created.append(path)

    return created


# ---------------------------------------------------------------------------
# Top-level verification
# ---------------------------------------------------------------------------

def run_verification(
    sru_probe: Callable[[str, str], Optional[dict]],
    lru_probe: Callable[[str, str], Optional[dict]],
    lifo_probe: Callable[[str], Optional[dict]],
    sru_pn: str = "TEST-SRU-001",
    lru_pn: str = "TEST-LRU-001",
    lifo_pn: str = "TEST-LIFO-001",
    output_dir: str = "tasks",
) -> VerificationReport:
    """Run both capability probes and generate stubs for any missing ones."""
    results: List[CapabilityResult] = [
        probe_sru_lru_compression(sru_pn, lru_pn, sru_probe, lru_probe),
        probe_lifo_teardown(lifo_pn, lifo_probe),
    ]

    missing = [r.capability for r in results if not r.compliant]
    addendum_files = generate_addendum_stubs(missing, output_dir) if missing else []

    return VerificationReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        capabilities=results,
        missing=missing,
        addendum_files_created=addendum_files,
    )


def _report_to_dict(report: VerificationReport) -> dict:
    """Serialize a report to plain dict with enum/datetime coercion."""
    return {
        "timestamp": report.timestamp,
        "capabilities": [
            {
                "capability": r.capability.value,
                "compliant": r.compliant,
                "detail": r.detail,
                "measured": r.measured,
            }
            for r in report.capabilities
        ],
        "missing": [c.value for c in report.missing],
        "addendum_files_created": list(report.addendum_files_created),
    }


def write_report(report: VerificationReport, path: str) -> None:
    """Serialize the VerificationReport to JSON at path."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(_report_to_dict(report), handle, indent=2, default=str)
        handle.write("\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _null_probe(*_args, **_kwargs):
    """Placeholder probe used by the CLI; real probes inject via tests."""
    return None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify V11 engine capabilities")
    parser.add_argument("--sru-pn", default="TEST-SRU-001")
    parser.add_argument("--lru-pn", default="TEST-LRU-001")
    parser.add_argument("--lifo-pn", default="TEST-LIFO-001")
    parser.add_argument("--output-dir", default="tasks")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    output_dir = args.output_dir if not args.dry_run else "/tmp/dryrun"

    report = run_verification(
        _null_probe,
        _null_probe,
        _null_probe,
        args.sru_pn,
        args.lru_pn,
        args.lifo_pn,
        output_dir,
    )
    write_report(report, os.path.join("/tmp", "engine-capability-report.json"))
    print(json.dumps(_report_to_dict(report), indent=2))
    return 0 if not report.missing else 2


if __name__ == "__main__":
    sys.exit(main())
