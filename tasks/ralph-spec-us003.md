# Implementation Spec: US-003 — /autoresearch skill — autonomous research-to-wiki loop

## Objective

Create a skill at `~/.claude/skills/autoresearch/SKILL.md` that runs a 3-round autonomous web research loop (search → gap analysis → synthesis) and files structured wiki pages into Alexandria via the existing `/ingest` skill.

## Acceptance Criteria

1. SKILL.md created at `~/.claude/skills/autoresearch/SKILL.md`
2. Accepts a topic string as argument: `/autoresearch 'IDG market pricing Q1 2026'`
3. Round 1: Runs 3-5 parallel WebSearch queries, fetches top sources via Jina, extracts key findings
4. Round 2: Identifies gaps in coverage (missing perspectives, contradictions, unanswered questions), runs targeted follow-up searches
5. Round 3: Synthesizes findings into structured wiki pages with YAML frontmatter, source citations, and cross-references
6. Files output pages into the correct Alexandria domain (auto-routes via ROUTING.md based on topic)
7. Each generated page includes a sources section with URLs and access dates
8. Calls /ingest internally for each source processed (reuses US-001 pipeline)
9. Appends research session summary to `~/alexandria/_system/logs/research-log.md`
10. Max 3 rounds to prevent runaway research loops; user can extend with `/autoresearch --continue`

## Files to Create

| File | Purpose |
|------|---------|
| `~/.claude/skills/autoresearch/SKILL.md` | The skill document — YAML frontmatter + 9-step research-to-wiki pipeline |

No other files. `research-log.md` is created on first invocation (same pattern as `ingestion-log.md` in /ingest).

## Patterns to Follow

### Frontmatter
```yaml
---
name: autoresearch
version: 1.0.1
description: |
  3-round autonomous web research loop that files wiki pages into Alexandria.
  Searches → gap analysis → synthesis → /ingest filing with citations.
  Trigger: /autoresearch, "research this", "deep dive on".
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - WebSearch
---
```

### Section order (mandatory)
1. `## Task` — 2-3 sentences
2. `## Trigger` — CLI usage block + argument table
3. `## Process` — `### Step N: verb phrase`, 9 steps, last 2 = domain log + skill-usage log
4. `## Output format` — one block per variant (normal, `--dry`, `--continue`)
5. `## Notes` — bulleted edge cases only

### Key patterns from US-001/002
- `printf '%s\n'` for append-only log writes (not `echo`)
- Dynamic domain list from `ls ~/alexandria/` — never hardcode
- URL validation with SSRF blocklist before Jina fetch
- Jina proxy: `curl -s "https://r.jina.ai/{URL}" -H "Accept: text/plain"`
- Idempotency check (slug existence) before writes
- Error format: `ERROR: {desc} — aborting.` / `WARN: {desc} — {consequence}.`
- Cap all unbounded loops with explicit ceiling + total count
- Version 1.0.1 (not 1.0.0)

### Composition with /ingest (AC-8 — CRITICAL)
- The skill MUST invoke `/ingest` via the Skill tool for filing wiki pages
- The skill MUST NOT contain inline `Write` calls for wiki pages — that's /ingest's job
- The skill handles: search, analysis, synthesis of content
- /ingest handles: wiki page creation, CONTEXT.md updates, cross-references, ingestion log
- The skill CAN write its own research-log.md (that's not wiki filing)

### 3-round structure
- **Round 1 (Search):** Decompose topic into 3-5 search queries. Run WebSearch in parallel. Fetch top 2-3 sources per query via Jina. Extract key findings into working notes.
- **Round 2 (Gap analysis):** Review Round 1 findings. Identify: missing perspectives, contradictions, unanswered questions. Generate 2-3 targeted follow-up queries. Run WebSearch + Jina fetch. Merge new findings.
- **Round 3 (Synthesis):** Organize all findings by subtopic. For each subtopic, invoke `/ingest` with synthesized content. Each page gets YAML frontmatter with sources section (URLs + access dates).
- **Max 3 rounds** enforced. `--continue` flag resumes from state file, adds 1-3 more rounds.
- **Convergence check:** If Round 2 finds <2 new unique facts, skip to Round 3 early.

### State for --continue
- State file: `/tmp/autoresearch-state-{slug}.json` with topic, round number, findings, sources
- `--continue` reads state, continues from stored round number
- Without `--continue`, fresh start (any existing state for that topic is overwritten)

### Source deduplication
- Track URLs seen across all rounds — never fetch the same URL twice
- Dedup key: normalized URL (strip trailing slash, lowercase hostname)

## Test/Verify Requirements

8 gates (structural verification, no pytest):

| Gate | Pass Condition |
|------|----------------|
| `file-exists` | `~/.claude/skills/autoresearch/SKILL.md` present, non-empty |
| `frontmatter` | `name: autoresearch`, version, description, allowed-tools includes WebSearch |
| `sections` | Task, Trigger, Process (Steps 1-9), Output Format, Notes headers present |
| `AC-coverage` | All 10 ACs map to named steps or guards |
| `composition-gate` | SKILL.md mentions invoking /ingest, does NOT contain inline `Write` calls for wiki pages |
| `loop-gate` | Round 1/2/3 structure + gap-analysis branch + max-rounds guard explicit |
| `citation-gate` | Output Format or page template shows sources section with URL + accessed date |
| `log-gate` | A step explicitly writes to `~/alexandria/_system/logs/research-log.md` |

## Gotchas

- Dry-run must exclude ALL writes: no Write, Edit, Bash mkdir/echo/printf redirects
- URL validation SSRF blocklist: 169.254.x.x, RFC 1918, file://, localhost
- `printf '%s\n'` over `echo` for untrusted content
- Dynamic domain list only — never hardcode domain names
- Batch /ingest calls are sequential (parallel causes cross-ref race conditions per /ingest Notes)
- WebSearch may return empty — handle gracefully, don't abort the round
- Jina fetch may fail — log warning, continue with available sources
- The `--continue` state file lives in /tmp (ephemeral) — document this limitation

## Out of Scope

- No pytest tests (skills are markdown, verified structurally)
- No changes to /ingest skill
- No changes to Alexandria infrastructure files
- No changes to Ralph itself
- No creation of research-log.md (created on first runtime invocation)
