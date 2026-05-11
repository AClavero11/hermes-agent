"""Dry-run Auto-think candidate queue for high-EV Hermes improvements."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hermes_constants import get_hermes_home
from hermes_cli.html_artifacts import ArtifactSection, HtmlArtifact, write_html_artifact


SOURCE_TYPES = {
    "x_link",
    "article",
    "canary_delta",
    "kanban_pattern",
    "workspace_inbox",
    "alexandria",
    "manual",
}
RISK_CLASSES = {
    "read_only",
    "draft_only",
    "internal_write",
    "prod_change",
    "external_action",
    "financial",
    "inventory",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
HARD_APPROVAL_GATES = (
    "customer/vendor sends",
    "quotes",
    "payments",
    "orders",
    "inventory/V11 mutations",
    "public posts",
    "destructive prod changes / destructive production changes",
    "paid signup",
    "untrusted installs",
)
ROUTE_BY_SCORE = (
    (9, "operator_prototype"),
    (7, "operator_prototype"),
    (5, "research"),
    (1, "file"),
)


class CandidateValidationError(ValueError):
    """Raised when an Auto-think candidate is incomplete or unsafe."""


@dataclass(frozen=True)
class Evidence:
    locator: str
    summary: str
    confidence: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Evidence":
        missing = [field_name for field_name in ("locator", "summary", "confidence") if not payload.get(field_name)]
        if missing:
            raise CandidateValidationError(f"evidence missing required fields: {', '.join(missing)}")
        confidence = str(payload["confidence"]).strip().lower()
        if confidence not in CONFIDENCE_LEVELS:
            raise CandidateValidationError("evidence.confidence must be high, medium, or low")
        return cls(
            locator=str(payload["locator"]).strip(),
            summary=str(payload["summary"]).strip(),
            confidence=confidence,
        )


@dataclass(frozen=True)
class EVScore:
    score: int
    relevance: int
    impact: int
    effort: int
    cost: int
    reliability: int
    privacy: int
    compounding: int
    rationale: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EVScore":
        required = ("score", "relevance", "impact", "effort", "cost", "reliability", "privacy", "compounding")
        missing = [f"ev.{field_name}" for field_name in required if field_name not in payload]
        if missing:
            raise CandidateValidationError(f"missing required fields: {', '.join(missing)}")
        values = {field_name: _score_int(payload[field_name], f"ev.{field_name}") for field_name in required}
        expected = score_ev(
            relevance=values["relevance"],
            impact=values["impact"],
            effort=values["effort"],
            cost=values["cost"],
            reliability=values["reliability"],
            privacy=values["privacy"],
            compounding=values["compounding"],
        )
        if values["score"] != expected:
            raise CandidateValidationError(f"ev.score must equal deterministic score {expected}")
        return cls(**values, rationale=str(payload.get("rationale") or "").strip())


@dataclass(frozen=True)
class AutoThinkCandidate:
    candidate_id: str
    source_type: str
    source_locator: str
    observed_at: str
    title: str
    core_idea: list[str]
    evidence: list[Evidence]
    dedupe_key: str
    affected_systems: list[str]
    risk_class: str
    approval_required: bool
    ev: EVScore
    recommended_route: str
    smallest_safe_prototype: str
    stop_gates: list[str]
    acceptance_criteria: list[str]
    status: str = "new"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AutoThinkCandidate":
        required = (
            "source_type",
            "source_locator",
            "title",
            "core_idea",
            "evidence",
            "dedupe_key",
            "affected_systems",
            "risk_class",
            "approval_required",
            "ev",
            "recommended_route",
            "smallest_safe_prototype",
            "stop_gates",
            "acceptance_criteria",
        )
        missing = [field_name for field_name in required if field_name not in payload]
        if missing:
            raise CandidateValidationError(f"missing required fields: {', '.join(missing)}")

        source_type = str(payload["source_type"]).strip().lower()
        if source_type not in SOURCE_TYPES:
            raise CandidateValidationError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
        source_locator = _required_text(payload, "source_locator")
        affected_systems = _text_list(payload.get("affected_systems"), "affected_systems")
        normalized_key = _normalize_freeform_key(str(payload["dedupe_key"]))
        expected_key = normalize_dedupe_key(source_type, source_locator, affected_systems)
        if normalized_key != expected_key:
            raise CandidateValidationError(f"dedupe_key must normalize to {expected_key}")

        risk_class = str(payload["risk_class"]).strip().lower()
        if risk_class not in RISK_CLASSES:
            raise CandidateValidationError(f"risk_class must be one of {sorted(RISK_CLASSES)}")
        approval_required = bool(payload["approval_required"])
        stop_gates = _text_list(payload.get("stop_gates"), "stop_gates")
        missing_gates = [gate for gate in HARD_APPROVAL_GATES if not _gate_is_covered(gate, stop_gates)]
        if missing_gates:
            raise CandidateValidationError(f"stop_gates missing hard approval gates: {', '.join(missing_gates)}")
        if risk_class in {"prod_change", "external_action", "financial", "inventory"} and not approval_required:
            raise CandidateValidationError("approval_required must be true for gated risk classes")

        observed_at = str(payload.get("observed_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        ev_score = EVScore.from_dict(dict(payload["ev"]))
        candidate_id = str(payload.get("candidate_id") or _candidate_id(observed_at, payload["title"], normalized_key))
        return cls(
            candidate_id=candidate_id,
            source_type=source_type,
            source_locator=source_locator,
            observed_at=observed_at,
            title=_required_text(payload, "title"),
            core_idea=_text_list(payload.get("core_idea"), "core_idea"),
            evidence=[Evidence.from_dict(dict(item)) for item in payload["evidence"]],
            dedupe_key=normalized_key,
            affected_systems=sorted(set(_normalize_token(item) for item in affected_systems)),
            risk_class=risk_class,
            approval_required=approval_required,
            ev=ev_score,
            recommended_route=str(payload["recommended_route"]).strip(),
            smallest_safe_prototype=_required_text(payload, "smallest_safe_prototype"),
            stop_gates=stop_gates,
            acceptance_criteria=_text_list(payload.get("acceptance_criteria"), "acceptance_criteria"),
            status=str(payload.get("status") or "new").strip().lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateStore:
    hermes_home: Path = field(default_factory=get_hermes_home)

    @property
    def path(self) -> Path:
        return Path(self.hermes_home) / "auto_think" / "candidates.jsonl"

    def existing_keys(self) -> set[str]:
        if not self.path.exists():
            return set()
        keys: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = payload.get("dedupe_key")
                if key:
                    keys.add(_normalize_freeform_key(str(key)))
        return keys

    def append(self, candidate: AutoThinkCandidate) -> bool:
        if candidate.dedupe_key in self.existing_keys():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(candidate.to_dict(), sort_keys=True) + "\n")
        return True

    def load_candidates(self) -> list[AutoThinkCandidate]:
        if not self.path.exists():
            return []
        candidates: list[AutoThinkCandidate] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                    candidates.append(AutoThinkCandidate.from_dict(payload))
                except (json.JSONDecodeError, CandidateValidationError):
                    continue
        return candidates


def write_candidate_dashboard(
    *,
    hermes_home: Path | None = None,
    output_path: Path | None = None,
    generated_at: str | None = None,
) -> Path:
    """Write a local-only HTML dashboard for the Auto-think candidate queue."""
    home = Path(hermes_home) if hermes_home else get_hermes_home()
    artifacts_dir = home / "html_artifacts"
    path = Path(output_path) if output_path else artifacts_dir / "auto-think-candidates-dashboard.html"
    _ensure_local_html_artifact_path(path, artifacts_dir)

    candidates = CandidateStore(home).load_candidates()
    timestamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    artifact = HtmlArtifact(
        title="Auto-think candidate dashboard",
        summary=_dashboard_summary(candidates),
        status="ready_for_review" if candidates else "empty_queue",
        sections=[
            _candidate_cards_section(candidates),
            _candidate_queue_section(candidates),
            ArtifactSection(
                heading="Generated timestamp",
                bullets=[f"Generated: {timestamp}"],
            ),
        ],
        approval_gates=_dashboard_approval_gates(candidates),
        provenance=_dashboard_provenance(candidates),
    )
    return write_html_artifact(artifact, path)


def dashboard_command(args: Any) -> Path:
    """Generate the Auto-think dashboard, print its path, and optionally open it locally."""
    hermes_home = Path(args.hermes_home) if getattr(args, "hermes_home", None) else get_hermes_home()
    artifact_path = write_candidate_dashboard(hermes_home=hermes_home)
    print(f"Artifact: {artifact_path}")
    if getattr(args, "open", False):
        if platform.system() != "Darwin":
            raise RuntimeError("--open is only supported on macOS")
        subprocess.run(["open", str(artifact_path)], check=False)
    return artifact_path


def score_ev(
    *,
    relevance: int,
    impact: int,
    effort: int,
    cost: int,
    reliability: int,
    privacy: int,
    compounding: int,
) -> int:
    """Return 1-10 EV score; low effort/cost are positive signals."""
    values = {
        "relevance": relevance,
        "impact": impact,
        "effort": effort,
        "cost": cost,
        "reliability": reliability,
        "privacy": privacy,
        "compounding": compounding,
    }
    for name, value in values.items():
        _score_int(value, name)
    positive_effort = 11 - effort
    positive_cost = 11 - cost
    weighted = (
        relevance * 2
        + impact * 2
        + positive_effort
        + positive_cost
        + reliability
        + privacy
        + compounding * 2
    ) / 10
    return max(1, min(10, round(weighted)))


def normalize_dedupe_key(source_type: str, source_locator: str, affected_systems: list[str]) -> str:
    normalized_type = _normalize_source_type(source_type)
    source_id = _normalize_source_locator(normalized_type, source_locator)
    systems = "-".join(sorted(set(_normalize_token(item) for item in affected_systems if str(item).strip())))
    return f"{normalized_type}:{source_id}:{systems}"


def render_operator_task_body(candidate: AutoThinkCandidate) -> str:
    gates = "\n".join(f"- {gate} require AC approval before execution." for gate in HARD_APPROVAL_GATES)
    criteria = "\n".join(f"- {item}" for item in candidate.acceptance_criteria)
    evidence = "\n".join(f"- {item.locator}: {item.summary} ({item.confidence})" for item in candidate.evidence)
    return "\n".join(
        [
            f"Implement dry-run Auto-think candidate: {candidate.title}",
            "",
            "Scope:",
            f"- Route: {candidate.recommended_route}",
            f"- Risk class: {candidate.risk_class}",
            f"- EV: {candidate.ev.score}/10",
            f"- Smallest safe prototype: {candidate.smallest_safe_prototype}",
            "- Mode: dry-run by default; no external sends, production mutations, or paid services.",
            "",
            "Evidence:",
            evidence,
            "",
            "Hard approval gates:",
            gates,
            "",
            "Stop gates:",
            "\n".join(f"- {gate}" for gate in candidate.stop_gates),
            "",
            "Acceptance criteria:",
            criteria,
            "",
            "Rollback:",
            "- Delete or ignore HERMES_HOME/auto_think/candidates.jsonl if noisy.",
            "- Keep prototype disabled unless explicitly invoked.",
            "- Remove the canary contract if it creates false positives.",
        ]
    )


def enqueue_candidate(
    payload: dict[str, Any],
    *,
    hermes_home: Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    normalized_payload = dict(payload)
    normalized_payload.setdefault(
        "dedupe_key",
        normalize_dedupe_key(
            str(normalized_payload.get("source_type") or ""),
            str(normalized_payload.get("source_locator") or ""),
            list(normalized_payload.get("affected_systems") or []),
        ),
    )
    ev_payload = dict(normalized_payload.get("ev") or {})
    if "score" not in ev_payload:
        ev_payload["score"] = score_ev(
            relevance=int(ev_payload.get("relevance", 1)),
            impact=int(ev_payload.get("impact", 1)),
            effort=int(ev_payload.get("effort", 10)),
            cost=int(ev_payload.get("cost", 10)),
            reliability=int(ev_payload.get("reliability", 1)),
            privacy=int(ev_payload.get("privacy", 1)),
            compounding=int(ev_payload.get("compounding", 1)),
        )
    normalized_payload["ev"] = ev_payload
    normalized_payload.setdefault("recommended_route", _route_for_score(int(ev_payload["score"])))

    candidate = AutoThinkCandidate.from_dict(normalized_payload)
    store = CandidateStore(Path(hermes_home) if hermes_home else get_hermes_home())
    written = False if dry_run else store.append(candidate)
    return {
        "dry_run": dry_run,
        "written": written,
        "store_path": str(store.path),
        "candidate": candidate.to_dict(),
        "operator_task_body": render_operator_task_body(candidate),
    }


def _dashboard_summary(candidates: list[AutoThinkCandidate]) -> str:
    count_label = "1 candidate" if len(candidates) == 1 else f"{len(candidates)} candidates"
    if not candidates:
        return "No Auto-think candidates found. Local dashboard is ready for queue review."
    return f"{count_label} queued for local operator review with EV scores, status, gates, and provenance."


def _candidate_cards_section(candidates: list[AutoThinkCandidate]) -> ArtifactSection:
    if not candidates:
        return ArtifactSection(
            heading="Candidate cards",
            summary="No Auto-think candidates found.",
            cards=[{"label": "Queue", "value": "No Auto-think candidates found."}],
        )
    cards = [
        {
            "label": f"EV {candidate.ev.score}/10 · {candidate.status}",
            "value": f"{candidate.title} | route: {candidate.recommended_route} | risk: {candidate.risk_class}",
        }
        for candidate in candidates
    ]
    return ArtifactSection(
        heading="Candidate cards",
        summary="Candidate list/cards with EV score, status, route, and risk class.",
        cards=cards,
    )


def _candidate_queue_section(candidates: list[AutoThinkCandidate]) -> ArtifactSection:
    if not candidates:
        return ArtifactSection(
            heading="Candidate queue",
            bullets=["No Auto-think candidates found.", "Source queue: HERMES_HOME/auto_think/candidates.jsonl"],
        )
    rows = [
        [
            candidate.candidate_id,
            candidate.title,
            f"EV {candidate.ev.score}/10",
            candidate.status,
            "Approval required" if candidate.approval_required else "No approval required",
            candidate.source_locator,
        ]
        for candidate in candidates
    ]
    return ArtifactSection(
        heading="Candidate queue",
        table={
            "headers": ["ID", "Title", "EV", "Status", "Gate", "Source"],
            "rows": rows,
        },
    )


def _dashboard_approval_gates(candidates: list[AutoThinkCandidate]) -> list[str]:
    if not candidates:
        return [
            "No candidate execution from dashboard render alone.",
            "Customer/vendor sends, quotes, payments, orders, inventory/V11 mutations, public posts, destructive prod changes, paid signup, and untrusted installs require AC approval.",
        ]
    gates: list[str] = []
    for candidate in candidates:
        gates.extend(candidate.stop_gates)
    return sorted(set(gates))


def _dashboard_provenance(candidates: list[AutoThinkCandidate]) -> list[str]:
    if not candidates:
        return ["source queue: HERMES_HOME/auto_think/candidates.jsonl"]
    provenance: list[str] = []
    for candidate in candidates:
        provenance.append(f"source: {candidate.source_locator}")
        provenance.extend(f"evidence: {item.locator} — {item.summary} ({item.confidence})" for item in candidate.evidence)
    return sorted(set(provenance))


def _ensure_local_html_artifact_path(path: Path, artifacts_dir: Path) -> None:
    resolved_path = path.expanduser().resolve()
    resolved_artifacts_dir = artifacts_dir.expanduser().resolve()
    if resolved_path.suffix.lower() != ".html":
        raise ValueError("dashboard output must be an .html file")
    try:
        resolved_path.relative_to(resolved_artifacts_dir)
    except ValueError as exc:
        raise ValueError("dashboard output must stay under HERMES_HOME/html_artifacts") from exc


def _route_for_score(score: int) -> str:
    for threshold, route in ROUTE_BY_SCORE:
        if score >= threshold:
            return route
    return "file"


def _score_int(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(f"{field_name} must be an integer 1-10") from exc
    if not 1 <= number <= 10:
        raise CandidateValidationError(f"{field_name} must be 1-10")
    return number


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise CandidateValidationError(f"{field_name} is required")
    return value


def _text_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CandidateValidationError(f"{field_name} must be a non-empty list")
    result = [str(item).strip() for item in value if str(item).strip()]
    if not result:
        raise CandidateValidationError(f"{field_name} must be a non-empty list")
    return result


def _normalize_source_type(source_type: str) -> str:
    normalized = str(source_type).strip().lower()
    if normalized in {"x_link", "twitter", "x"}:
        return "x"
    return normalized.replace("_link", "")


def _normalize_source_locator(source_type: str, source_locator: str) -> str:
    locator = str(source_locator).strip()
    if source_type == "x":
        match = re.search(r"/status(?:es)?/(\d+)", locator)
        if match:
            return match.group(1)
    parsed = urlparse(locator)
    if parsed.netloc:
        return _normalize_token(f"{parsed.netloc}{parsed.path}".strip("/"))
    return _normalize_token(locator)


def _normalize_freeform_key(value: str) -> str:
    return ":".join(_normalize_token(part) for part in str(value).strip().split(":"))


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")


def _candidate_id(observed_at: str, title: str, dedupe_key: str) -> str:
    date = re.sub(r"[^0-9]", "", observed_at[:10]) or datetime.now(timezone.utc).strftime("%Y%m%d")
    slug = _normalize_token(title)[:40] or "candidate"
    digest = hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()[:8]
    return f"auto_think_{date}_{slug}_{digest}"


def _gate_is_covered(gate: str, stop_gates: list[str]) -> bool:
    gate_variants = [part.strip() for part in gate.split("/") if part.strip()]
    for variant in gate_variants:
        gate_terms = set(_normalize_token(variant).split("-"))
        for stop_gate in stop_gates:
            terms = set(_normalize_token(stop_gate).split("-"))
            if gate_terms.issubset(terms):
                return True
    return False
