# Hermes 10/10 Overhaul

Purpose: rebuild Hermes from a chat-model agent into an operator-grade business control plane.

## Target Definition

| Capability | 10/10 requirement |
|---|---|
| Correctness | Does the requested task, not an adjacent task, with deterministic verification for facts, math, state changes, and sends |
| Grounding | Every business answer cites live source evidence: V11/Alexandria/customer history/file/log/report path |
| Persistence | Task state survives restarts, Telegram reconnects, model timeouts, and device changes without stale continuation leakage |
| Observability | Operator can inspect active task, model route, tools used, evidence, failure class, next action, runtime SHA, and logs |
| Recovery | Gateway/model/tool/network failures degrade into bounded recovery actions and Workspace blockers, not silent hangs |
| Business autonomy | Hermes prepares quotes, follow-ups, inventory targets, reports, and approval packets; external sends and production writes are approval-gated |
| Measurement | Release score is capped unless current-SHA live Telegram, model, runtime, RFQ, and failure-recovery canaries pass |

Unshackled means no fake model refusals, no unnecessary waiting, no stale session loops, and no manual babysitting for internal work. It does not mean unguarded customer sends, destructive shell, secret exposure, or production ERP writes.

## Core Architecture

| Layer | Role | Model/tool policy |
|---|---|---|
| Router | Classifies prompt, risk, lane, required evidence, and approval level before any model call | Deterministic Python first |
| Planner | Breaks ambiguous goals into bounded steps and success criteria | Strongest available frontier route; fallback to local only with smaller scope |
| Executor | Runs retrieval, file ops, V11 reads, draft generation, code edits, and tool calls | Local DeepSeek/Kimi for bounded structured subtasks |
| Verifier | Checks math, citations, schema, state changes, no-send/no-write guards, and expected customer/business constraints | Deterministic code plus judge model for ambiguity |
| Synthesizer | Produces final operator-facing answer from verified evidence | Planner/synth model; never raw tool-executor output |
| Supervisor | Tracks task state, timeouts, retries, restart recovery, and progress updates | Deterministic state machine |

The model is not the agent. The harness is the agent. Smaller models look powerful when the harness gives them narrow jobs, clean state, strong tools, and hard verification.

## Workstream 1: Runtime Truth

| Item | Acceptance criteria |
|---|---|
| Single Studio runtime | One active runtime path, one rollback path, one launchd service, one health URL, one deploy script |
| Runtime status | `hermes runtime status` reports launchd label, cwd, wrapper-selected repo, Python path, runtime home, model provider/name, health URL, git SHA, dirty flag, active agents, Telegram webhook |
| Drift failure | Score caps below 8.0 if runtime metadata, launchd process, health endpoint, or wrapper-selected repo disagree |
| Scheduled proof | Daily canary is launchd-fired, logs stdout/stderr freshness, and records current runtime SHA |

## Workstream 2: Telegram Operator UX

| Item | Acceptance criteria |
|---|---|
| Fast path prompts | `ack`, `testing`, `what can we do`, `rfq`, `inventory`, `followups`, `hermes`, `/status`, `/stop`, `/new` answer in under 2 seconds |
| Task lanes | Major lane switch starts a fresh task/session or explicit Workspace task to prevent semantic contamination |
| Busy behavior | Long tasks show current step, model/tool, elapsed, last event, next timeout, and interruption commands |
| Visible proof | Release above 9.0 requires current-SHA Telegram send-path proof for exact operator prompts and human-visible delivery proof within TTL |
| Malformed output block | Capability refusals, provider aliases as commands, bare filename terminal calls, quote-refusal loops, and unbounded continuation dumps are blocked before delivery |

## Workstream 3: Model Routing

| Task type | Route |
|---|---|
| Deterministic extraction, formatting, simple file ops | Local DeepSeek/Kimi executor |
| Code edits | Coding model plus tests |
| Ambiguous planning, root-cause debugging, business decisions | Strongest available frontier planner |
| Numeric quote math, discounts, margins, totals | Deterministic calculators and schema validators |
| Long autonomous goal supervision | Deterministic supervisor plus separate judge/verifier |
| Final operator answer | Synthesizer using verified evidence, not raw executor output |

Acceptance criteria:
- Runtime route resolver exports actual provider/model availability, not just config aliases.
- Frontier fallback is real in production routing, not only the canary.
- Local DeepSeek reasoning failures are release-failing under strict profile.
- Any quote/math output includes deterministic recomputation evidence.

## Workstream 4: Workspace as Source of Truth

| Rule | Acceptance criteria |
|---|---|
| No nontrivial work without task | `/goal`, code edits, quote packages, follow-ups, reports, and failures attach to a Workspace task |
| No done without evidence | Completion requires tests/logs/report path/V11 query evidence/Telegram proof as applicable |
| No failure without next action | Failures create blocker with class, evidence, owner, allowed auto-actions, and retry policy |
| No external send without approval | Customer emails/messages/social posts/production writes require approval artifact and audit trail |

## Workstream 5: RFQ and Quote Autonomy

| Stage | 10/10 requirement |
|---|---|
| RFQ intake | Pull live RFQs/stale quotes, extract part/qty/customer/terms/condition, detect missing fields |
| Evidence | Attach V11 inventory, part description, trace/8130 status, customer history, pricing comps, margin policy, and lead time |
| Draft package | Produce customer-ready quote draft, internal margin note, QAMFORM preview, approval card, and follow-up schedule |
| Approval | AC approves exact draft/send/write packet in Telegram or Workspace |
| Production action | After approval only: create/write approved records, send customer email, attach audit proof, schedule follow-up, surface exceptions |

10/10 is not "auto-send everything." It is "Hermes can run the quote process end-to-end with explicit approval gates and a complete audit trail."

## Workstream 6: Evaluation Ladder

| Gate | Cap if missing |
|---|---:|
| Runtime SHA/health/wrapper agreement | 8.0 |
| Live `/v1/responses` behavior goldens | 8.2 |
| Hermes reasoning eval | 8.5 |
| Telegram operator prompt send-path proof | 8.8 |
| Human-visible Telegram delivery proof | 8.9 |
| Frontier planner/judge route proof | 8.9 |
| Quote services/PDF/approval bot health | 9.2 |
| Multi-case RFQ dry-run with citations | 9.4 |
| Approved draft/write packet proof | 9.6 |
| Approved customer-send + audit + follow-up proof | 10.0 |

Release profile must treat skipped live checks as failures. Daily low-noise canaries may skip visible probes, but they cannot publish above-9 quality.

## Workstream 7: Observability

| Surface | Must show |
|---|---|
| `hermes status` | runtime path, SHA, services, model route, Telegram webhook, active tasks, latest score, open blockers |
| `hermes task <id>` | current step, evidence, model calls, tool calls, approvals, failure class, next action |
| Daily report | pass/fail trend, latency trend, top regressions, generated Workspace tasks |
| Telegram `/status` | concise current task state and commands, not generic chatbot text |

## Sequenced Execution

| Phase | Scope | Done when |
|---|---|---|
| 0 | Runtime truth and score honesty | Current deploy cannot claim above 9 without current Telegram proof and runtime agreement |
| 1 | Telegram operator rebuild | Short prompts, menu choices, busy task status, `/new`, `/stop`, and malformed-output block pass live canaries |
| 2 | Model router rebuild | Planner/executor/verifier/synthesizer routes are explicit and canary-proven |
| 3 | Workspace task engine | Every nontrivial task has state, evidence, failures, approvals, and recovery |
| 4 | RFQ quote autopilot | Multi-case quote packages are sourced, deterministic, approval-ready, and guarded |
| 5 | 10.0 approved-send loop | Approved customer send/write/audit/follow-up succeeds in controlled production path |
| 6 | Dashboard and weekly trend | Dashboard reflects the same facts as canary/runtime/Workspace; no decorative duplicate truth |

## Immediate Next Commit Targets

1. Add strict release profile: skipped live checks fail, score caps enforce current-SHA evidence TTLs.
2. Add Telegram `/new`, `/stop`, and `/status` deterministic operator commands.
3. Add task-lane partitioning so `rfq`, `inventory`, `followups`, and `hermes` cannot reuse poisoned planner history.
4. Promote frontier fallback from canary-only into actual planner routing.
5. Add multi-case RFQ dry-run canary with deterministic quote math and citations.
