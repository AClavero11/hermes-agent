"""Workflow templates for Hermes Kanban.

Templates create small DAGs of specialist-profile tasks. They intentionally
stay in the Kanban layer so CLI, slash commands, and agent tools share one
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from hermes_cli import kanban_db as kb


@dataclass(frozen=True)
class WorkflowStep:
    key: str
    title: str
    assignee: str
    body: str
    parents: tuple[str, ...] = ()
    priority_delta: int = 0
    max_runtime_seconds: Optional[int] = None
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    name: str
    description: str
    steps: tuple[WorkflowStep, ...] = field(default_factory=tuple)


TEMPLATES: dict[str, WorkflowTemplate] = {
    "research-brief": WorkflowTemplate(
        id="research-brief",
        name="Research Brief",
        description="Source-grounded research with chief-of-staff synthesis.",
        steps=(
            WorkflowStep(
                key="research",
                title="Research and evidence collection",
                assignee="research",
                body=(
                    "Return source-backed findings, confidence level, risks, "
                    "and recommended AAC action. Include URLs or Alexandria/V11 "
                    "source names for every material claim."
                ),
                priority_delta=10,
            ),
            WorkflowStep(
                key="synthesis",
                title="Synthesize research into AC-ready brief",
                assignee="chief-of-staff",
                body=(
                    "Read parent findings, decide what matters, and return "
                    "bottom line, risks, and next action. Do not expand scope."
                ),
                parents=("research",),
            ),
        ),
    ),
    "operator-fix": WorkflowTemplate(
        id="operator-fix",
        name="Operator Fix",
        description="Debug/patch/verify a technical issue, then summarize risk.",
        steps=(
            WorkflowStep(
                key="diagnose-fix",
                title="Diagnose, fix, and verify technical issue",
                assignee="operator",
                body=(
                    "Inspect current state first, identify root cause, make "
                    "scoped changes only, run focused verification, and record "
                    "files changed plus commands run."
                ),
                priority_delta=10,
                max_runtime_seconds=60 * 60,
            ),
            WorkflowStep(
                key="review-handoff",
                title="Review fix handoff and residual risk",
                assignee="chief-of-staff",
                body=(
                    "Read the operator handoff and produce AC-ready status: "
                    "changed, verified, residual risk, and next decision."
                ),
                parents=("diagnose-fix",),
            ),
        ),
    ),
    "rfq-package": WorkflowTemplate(
        id="rfq-package",
        name="RFQ Package",
        description="Prepare a quote package with V11/Alexandria facts, market context, and approval gate.",
        steps=(
            WorkflowStep(
                key="v11-intake",
                title="RFQ intake and V11/Alexandria lookup",
                assignee="sales-rfq",
                body=(
                    "Extract part numbers, customer, condition, qty, lead time, "
                    "cert requirements, and V11/Alexandria facts. No customer send "
                    "or quote issuance without AC approval."
                ),
                priority_delta=20,
            ),
            WorkflowStep(
                key="market-check",
                title="Market/vendor availability check",
                assignee="research",
                body=(
                    "Find market/vendor context, comps, alternates, and risk "
                    "signals. Cite sources and flag stale or weak evidence."
                ),
                priority_delta=10,
            ),
            WorkflowStep(
                key="finance-check",
                title="Credit/payment/admin review",
                assignee="finance-admin",
                body=(
                    "Check finance/admin context needed before quote package: "
                    "customer terms, credit/payment flags, invoice/admin concerns. "
                    "Draft only; no financial action."
                ),
                parents=("v11-intake",),
            ),
            WorkflowStep(
                key="quote-package",
                title="Assemble quote package for AC approval",
                assignee="sales-rfq",
                body=(
                    "Combine parent findings into a quote-ready package: facts, "
                    "missing fields, pricing/margin considerations, draft customer "
                    "response, and explicit approval needed."
                ),
                parents=("v11-intake", "market-check", "finance-check"),
            ),
        ),
    ),
    "finance-follow-up": WorkflowTemplate(
        id="finance-follow-up",
        name="Finance Follow-Up",
        description="Reconcile/admin prep with a drafted follow-up and approval gate.",
        steps=(
            WorkflowStep(
                key="reconcile",
                title="Reconcile finance/admin item",
                assignee="finance-admin",
                body=(
                    "Match records by invoice/SO/PO/customer/vendor/date/amount. "
                    "Return exceptions, evidence, and proposed action. No payments "
                    "or entries without AC approval."
                ),
                priority_delta=10,
            ),
            WorkflowStep(
                key="draft-follow-up",
                title="Draft follow-up message or reminder",
                assignee="calendar-email",
                body=(
                    "Use parent findings to draft concise follow-up text and "
                    "timing. Do not send or create commitments without AC approval."
                ),
                parents=("reconcile",),
            ),
            WorkflowStep(
                key="approval-summary",
                title="Summarize finance follow-up for AC approval",
                assignee="chief-of-staff",
                body=(
                    "Return bottom line, exceptions, drafted action, and the "
                    "approval decision needed from AC."
                ),
                parents=("draft-follow-up",),
            ),
        ),
    ),
}


def list_templates() -> list[dict[str, Any]]:
    return [
        {
            "id": template.id,
            "name": template.name,
            "description": template.description,
            "steps": [
                {
                    "key": step.key,
                    "title": step.title,
                    "assignee": step.assignee,
                    "parents": list(step.parents),
                }
                for step in template.steps
            ],
        }
        for template in TEMPLATES.values()
    ]


def get_template(template_id: str) -> WorkflowTemplate:
    key = str(template_id or "").strip().lower()
    if key not in TEMPLATES:
        raise ValueError(
            f"unknown workflow template {template_id!r}; "
            f"known templates: {', '.join(sorted(TEMPLATES))}"
        )
    return TEMPLATES[key]


def create_workflow(
    conn,
    *,
    template_id: str,
    title: str,
    body: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    """Create all tasks for a workflow template and return a task map."""
    template = get_template(template_id)
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")

    created: dict[str, str] = {}
    tasks: list[dict[str, Any]] = []
    for step in template.steps:
        parent_ids = [created[parent_key] for parent_key in step.parents]
        step_body = _render_step_body(
            workflow_title=title,
            workflow_body=body,
            template=template,
            step=step,
        )
        step_idempotency_key = (
            f"{idempotency_key}:{step.key}" if idempotency_key else None
        )
        task_id = kb.create_task(
            conn,
            title=f"{title} - {step.title}",
            body=step_body,
            assignee=step.assignee,
            created_by=created_by,
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            tenant=tenant,
            priority=int(priority) + int(step.priority_delta),
            parents=parent_ids,
            idempotency_key=step_idempotency_key,
            max_runtime_seconds=step.max_runtime_seconds,
            skills=step.skills or None,
            workflow_template_id=template.id,
            current_step_key=step.key,
        )
        # create_task returns early on idempotency hits, so ensure dependency
        # edges exist when a retried workflow resumes from partial creation.
        for parent_id in parent_ids:
            if parent_id not in kb.parent_ids(conn, task_id):
                kb.link_tasks(conn, parent_id, task_id)
        created[step.key] = task_id
        task = kb.get_task(conn, task_id)
        tasks.append({
            "key": step.key,
            "task_id": task_id,
            "title": task.title if task else f"{title} - {step.title}",
            "assignee": step.assignee,
            "status": task.status if task else None,
            "parents": parent_ids,
        })

    return {
        "template_id": template.id,
        "template_name": template.name,
        "title": title,
        "tasks": tasks,
        "task_ids": [task["task_id"] for task in tasks],
    }


def _render_step_body(
    *,
    workflow_title: str,
    workflow_body: Optional[str],
    template: WorkflowTemplate,
    step: WorkflowStep,
) -> str:
    parts = [
        f"Workflow: {template.name} ({template.id})",
        f"Goal: {workflow_title}",
        f"Step: {step.key} - {step.title}",
    ]
    if workflow_body:
        parts.extend(["", "Workflow context:", str(workflow_body).strip()])
    parts.extend(["", "Step instructions:", step.body])
    return "\n".join(parts).strip()
