"""Local HTML artifact rendering for long Hermes operator handoffs.

This module is intentionally narrow: it creates static, local HTML files for
internal specs/reports only. It does not publish, upload, send, or replace short
Markdown/Telegram responses.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArtifactSection:
    heading: str
    summary: str = ""
    cards: list[dict[str, Any]] = field(default_factory=list)
    table: dict[str, Any] | None = None
    bullets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HtmlArtifact:
    title: str
    summary: str
    status: str
    sections: list[ArtifactSection]
    approval_gates: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)
    workflow_svg: str = ""


def render_html_artifact(artifact: HtmlArtifact) -> str:
    """Render a deterministic, dependency-free HTML artifact string."""
    nav_items = "\n".join(
        f'<a href="#{_section_id(section.heading)}">{_escape(section.heading)}</a>'
        for section in artifact.sections
    )
    sections = "\n".join(_render_section(section) for section in artifact.sections)
    workflow = _render_workflow_svg(artifact.workflow_svg)
    approval_gates = _render_list(artifact.approval_gates, empty="No risky action gates supplied.")
    provenance = _render_list(artifact.provenance, empty="No source/provenance supplied.")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(artifact.title)}</title>
  <style>
{_CSS}
  </style>
</head>
<body>
  <main class="artifact-shell">
    <header class="hero">
      <div>
        <p class="eyebrow">Hermes internal artifact</p>
        <h1>{_escape(artifact.title)}</h1>
        <p class="summary">{_escape(artifact.summary)}</p>
      </div>
      <aside class="status-card">
        <span>Status</span>
        <strong>{_escape(artifact.status)}</strong>
      </aside>
    </header>
    <nav class="section-nav" aria-label="Artifact sections">
      {nav_items}
      <a href="#approval-gates">Approval gates</a>
      <a href="#provenance">Source / provenance</a>
    </nav>
    {workflow}
    {sections}
    <section id="approval-gates" class="panel approval-panel">
      <h2>Approval gates</h2>
      {approval_gates}
    </section>
    <footer id="provenance" class="panel provenance">
      <h2>Source / provenance</h2>
      {provenance}
      <p class="footer-note">Static local file. No external CDN, upload, public publish, customer send, V11 write, or production mutation.</p>
    </footer>
  </main>
</body>
</html>
"""


def write_html_artifact(artifact: HtmlArtifact, output_path: Path) -> Path:
    """Write an HTML artifact to a local path and return the path."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html_artifact(artifact), encoding="utf-8")
    return path


def write_sample_artifact(output_dir: Path) -> Path:
    """Write the deterministic sample artifact used for smoke tests and demos."""
    artifact = HtmlArtifact(
        title="Hermes HTML artifact standard",
        summary="Preferred local format for long internal operator specs, reports, dashboards, and handoffs.",
        status="draft_internal",
        workflow_svg=(
            '<svg viewBox="0 0 560 100" role="img" aria-label="HTML artifact workflow">'
            '<rect x="10" y="30" width="120" height="40" rx="8" fill="#1f6feb"/>'
            '<text x="70" y="56" text-anchor="middle" fill="white">Collect</text>'
            '<path d="M140 50 H210" stroke="#94a3b8" stroke-width="4" marker-end="url(#arrow)"/>'
            '<rect x="220" y="30" width="120" height="40" rx="8" fill="#0f766e"/>'
            '<text x="280" y="56" text-anchor="middle" fill="white">Render</text>'
            '<path d="M350 50 H420" stroke="#94a3b8" stroke-width="4" marker-end="url(#arrow)"/>'
            '<rect x="430" y="30" width="120" height="40" rx="8" fill="#7c3aed"/>'
            '<text x="490" y="56" text-anchor="middle" fill="white">Review</text>'
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"/></marker></defs>'
            '</svg>'
        ),
        sections=[
            ArtifactSection(
                heading="Use cases",
                cards=[
                    {"label": "Operator handoffs", "value": "Long Kanban completions and downstream review packets."},
                    {"label": "Specs/reports", "value": "Auto-think, Auto-build, RFQ, stale quote, and teardown dashboards."},
                    {"label": "Not targeted", "value": "Short Telegram replies and customer-facing documents."},
                ],
            ),
            ArtifactSection(
                heading="Minimum structure",
                table={
                    "headers": ["Block", "Purpose"],
                    "rows": [
                        ["title/summary/status", "Fast operator scan"],
                        ["section navigation", "Non-linear reading"],
                        ["cards/tables", "Dense facts without markdown walls"],
                        ["approval gates", "Visible stop conditions before risky action"],
                        ["source/provenance", "Grounding and audit trail"],
                    ],
                },
            ),
        ],
        approval_gates=[
            "No customer/vendor sends without explicit AC approval.",
            "No quote issuance, payment, order, inventory/V11 mutation, public post, upload, or production routing change.",
            "Use local static files only unless AC explicitly approves publication or delivery.",
        ],
        provenance=["task: t_183b1d98", "source: https://x.com/trq212/status/2052809885763747935?s=46"],
    )
    return write_html_artifact(artifact, Path(output_dir) / "hermes-html-artifact-sample.html")


def _render_workflow_svg(workflow_svg: str) -> str:
    if not workflow_svg.strip():
        return ""
    return f'<section class="panel workflow"><h2>Workflow diagram</h2>{_safe_svg(workflow_svg)}</section>'


def _render_section(section: ArtifactSection) -> str:
    cards = "" if not section.cards else f'<div class="cards">{"".join(_render_card(card) for card in section.cards)}</div>'
    table = "" if not section.table else _render_table(section.table)
    bullets = "" if not section.bullets else _render_list(section.bullets)
    summary = "" if not section.summary else f'<p class="section-summary">{_escape(section.summary)}</p>'
    return (
        f'<section id="{_section_id(section.heading)}" class="panel">'
        f'<h2>{_escape(section.heading)}</h2>'
        f'{summary}{cards}{table}{bullets}'
        '</section>'
    )


def _render_card(card: dict[str, Any]) -> str:
    label = _escape(str(card.get("label", "")))
    value = _escape(str(card.get("value", "")))
    return f'<article class="card"><span>{label}</span><strong>{value}</strong></article>'


def _render_table(table: dict[str, Any]) -> str:
    headers = [str(header) for header in table.get("headers", [])]
    rows = [[str(cell) for cell in row] for row in table.get("rows", [])]
    header_html = "".join(f"<th>{_escape(header)}</th>" for header in headers)
    rows_html = "\n".join(
        "<tr>" + "".join(f"<td>{_escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table></div>'


def _render_list(items: list[str], *, empty: str = "No entries.") -> str:
    if not items:
        return f"<p>{_escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in items) + "</ul>"


def _safe_svg(svg: str) -> str:
    stripped = svg.strip()
    if not re.match(r"^<svg[\s>]", stripped, flags=re.IGNORECASE):
        return ""
    if re.search(r"<script|on[a-z]+\s*=|javascript:", stripped, flags=re.IGNORECASE):
        return ""
    return stripped


def _section_id(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")
    return slug or "section"


def _escape(value: str) -> str:
    return html.escape(str(value), quote=True)


_CSS = """    :root {
      color-scheme: light dark;
      --bg: #0f172a;
      --panel: #111827;
      --panel-soft: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --accent: #38bdf8;
      --danger: #f97316;
      --line: #334155;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    .artifact-shell { max-width: 1120px; margin: 0 auto; padding: 32px 20px 56px; }
    .hero { display: grid; grid-template-columns: 1fr minmax(180px, 260px); gap: 20px; align-items: stretch; }
    .eyebrow { color: var(--accent); font-size: 0.8rem; font-weight: 700; letter-spacing: 0.12em; margin: 0 0 8px; text-transform: uppercase; }
    h1 { font-size: clamp(2rem, 5vw, 4rem); line-height: 1; margin: 0; }
    h2 { margin: 0 0 16px; }
    .summary { color: var(--muted); font-size: 1.08rem; max-width: 760px; }
    .status-card, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 20px 60px rgba(0,0,0,.22); }
    .status-card { padding: 22px; display: flex; flex-direction: column; justify-content: center; }
    .status-card span, .card span { color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .08em; }
    .status-card strong { color: var(--accent); font-size: 1.35rem; margin-top: 8px; }
    .section-nav { display: flex; flex-wrap: wrap; gap: 10px; margin: 24px 0; }
    .section-nav a { color: var(--text); text-decoration: none; background: var(--panel-soft); border: 1px solid var(--line); padding: 8px 12px; border-radius: 999px; }
    .panel { margin-top: 18px; padding: 22px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
    .card { background: var(--panel-soft); border: 1px solid var(--line); border-radius: 14px; padding: 16px; }
    .card strong { display: block; margin-top: 8px; line-height: 1.35; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 11px 10px; text-align: left; vertical-align: top; }
    th { color: var(--accent); }
    li { margin: 8px 0; }
    .approval-panel { border-color: var(--danger); }
    .workflow svg { width: 100%; height: auto; background: #020617; border: 1px solid var(--line); border-radius: 14px; }
    .provenance { color: var(--muted); }
    .footer-note { border-top: 1px solid var(--line); margin-top: 18px; padding-top: 14px; }
    @media (max-width: 720px) { .hero { grid-template-columns: 1fr; } .artifact-shell { padding: 20px 12px 40px; } }
"""
