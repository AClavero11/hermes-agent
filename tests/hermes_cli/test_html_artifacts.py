from __future__ import annotations

from pathlib import Path

from hermes_cli import html_artifacts


def test_render_html_artifact_contains_required_operator_sections():
    artifact = html_artifacts.HtmlArtifact(
        title="Auto-build spec handoff",
        summary="Convert long operator handoffs into readable local HTML.",
        status="draft_internal",
        sections=[
            html_artifacts.ArtifactSection(
                heading="Scope",
                cards=[
                    {"label": "Format", "value": "local .html artifact"},
                    {"label": "External sends", "value": "blocked"},
                ],
            ),
            html_artifacts.ArtifactSection(
                heading="Verification",
                table={
                    "headers": ["Check", "Result"],
                    "rows": [["sample render", "pass"], ["CDN dependency", "none"]],
                },
            ),
        ],
        approval_gates=[
            "No customer/vendor sends without AC approval",
            "No V11 mutations without AC approval",
        ],
        provenance=["source: https://x.com/trq212/status/2052809885763747935?s=46"],
        workflow_svg='<svg viewBox="0 0 100 20"><text x="1" y="15">draft → approve</text></svg>',
    )

    rendered = html_artifacts.render_html_artifact(artifact)

    assert rendered.startswith("<!doctype html>")
    assert "Auto-build spec handoff" in rendered
    assert "Convert long operator handoffs" in rendered
    assert 'href="#scope"' in rendered
    assert "draft_internal" in rendered
    assert "No customer/vendor sends without AC approval" in rendered
    assert "No V11 mutations without AC approval" in rendered
    assert "source: https://x.com/trq212/status/2052809885763747935?s=46" in rendered
    assert "&lt;svg" not in rendered
    assert "<svg viewBox=\"0 0 100 20\">" in rendered
    assert "https://cdn" not in rendered


def test_write_sample_html_artifact_is_deterministic(tmp_path: Path):
    output_path = html_artifacts.write_sample_artifact(tmp_path)

    assert output_path == tmp_path / "hermes-html-artifact-sample.html"
    first = output_path.read_text(encoding="utf-8")
    second_path = html_artifacts.write_sample_artifact(tmp_path)
    second = second_path.read_text(encoding="utf-8")

    assert first == second
    assert "Hermes HTML artifact standard" in first
    assert "Approval gates" in first
    assert "Source / provenance" in first
