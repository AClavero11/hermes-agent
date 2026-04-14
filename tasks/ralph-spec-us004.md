# Implementation Spec: US-004 — Autoreason in Ralph REVIEW phase

## Objective

Replace the single Sonnet adversarial reviewer in Ralph Phase 6 (REVIEW) with an A/B/AB blind Borda count protocol based on NousResearch/autoreason. The new protocol produces three implementation variants, has two blind judges rank them, and iterates until convergence or cap.

## Acceptance Criteria

1. Ralph SKILL.md Phase 6 (REVIEW) updated to use Autoreason protocol
2. Three versions produced: A (unchanged implementation), B (adversarial revision by fresh Sonnet agent), AB (synthesis of A and B by third agent)
3. Blind Borda count: 2 fresh judge agents (no shared context with A/B/AB producers) rank all three versions without knowing which is which
4. If A (do-nothing) wins 2 consecutive rounds, loop stops — implementation is good enough
5. If B or AB wins, that version replaces A and the loop continues (max 3 rounds)
6. Judge prompt includes acceptance criteria and gate results but NOT which version is incumbent
7. All versions and judge scores logged to tasks/ralph-autoreason-[story-id].md for auditability
8. Fallback: if Autoreason adds >5 minutes to REVIEW, degrade to current single-reviewer mode with a log entry

## File to Modify

`~/.claude/skills/ralph/SKILL.md` (843 lines) — 7 edit points

## Edit Points (in line order)

### Edit 1: Frontmatter description (lines 4-8)
Change "adversarial review" to mention Autoreason A/B/AB blind Borda review.

### Edit 2: State JSON example (lines 59-71)
Add to phaseRetries: `"autoReasonRound": 0, "consecutiveAWins": 0`

### Edit 3: Phase 6 — FULL REPLACEMENT (lines 492-571)
Replace the entire Phase 6 section with the Autoreason protocol:

```
## Phase 6: REVIEW (Autoreason Protocol)

Replaces single-reviewer with A/B/AB blind Borda count (NousResearch/autoreason).
Three implementation variants are produced, two blind judges rank them, and the
winner becomes the new baseline. Converges when A wins twice or after 3 rounds.

### 6a. Setup

- Record `REVIEW_START=$(date +%s)` for 5-minute timeout tracking
- Capture current implementation diff: `A_DIFF=$(git -C "$WT" diff "$MERGE_BASE"..HEAD)`
- Read changed files list: `CHANGED_FILES=$(git -C "$WT" diff --name-only "$MERGE_BASE"..HEAD)`
- For each changed file, read full content → store as `A_FILES` map (path → content)
- Initialize: `consecutive_a_wins = 0`, `autoreason_round = 0`
- Create autoreason log: Write `tasks/ralph-autoreason-[story-id].md` with header:
  ```
  # Autoreason Log: [Story ID] — [title]
  AC: [list]
  Gate results: [summary]
  ```

### 6b. Autoreason Round Loop (max 3 rounds)

**Timeout check** at loop start: `ELAPSED = $(date +%s) - REVIEW_START`. If ELAPSED > 300:
- Log `[FALLBACK: timeout at {ELAPSED}s — degrading to single-reviewer]` to autoreason log
- Fall back to original single-reviewer (the prompt preserved in section 6d below)
- Proceed with single-reviewer verdict
- Break loop

Increment `autoreason_round`.

**Step 1: Produce Variant B** (Sonnet, foreground, read-only)

Agent tool:
  model: "sonnet"
  prompt: "You are an adversarial code reviser for the Autoreason protocol.
  You receive an implementation (Variant A) and its acceptance criteria.
  Your goal: produce an IMPROVED version that fixes any issues, adds missing
  edge cases, and improves code quality.

  ACCEPTANCE CRITERIA:
  [list from prd.json]

  GATE RESULTS:
  [Gate Summary from VERIFY]

  CURRENT IMPLEMENTATION (Variant A):
  [For each file in A_FILES: show filename + full content]

  RULES:
  1. Output the COMPLETE revised file for each changed file
  2. Format: ### filename\n```\n{content}\n```
  3. If a file needs no changes, still output it unchanged
  4. Focus on: correctness, missing AC coverage, edge cases, security
  5. Do NOT add features beyond the AC
  6. Do NOT explain your changes — just output the files"

Capture B-worker output → parse into `B_FILES` map.

**Step 2: Produce Variant AB** (Sonnet, foreground, read-only)

Agent tool:
  model: "sonnet"
  prompt: "You are a code synthesizer for the Autoreason protocol.
  You receive two implementation variants (A and B) of the same feature.
  Produce a synthesis that combines the best aspects of both.

  ACCEPTANCE CRITERIA:
  [list from prd.json]

  VARIANT A (original):
  [For each file in A_FILES: show filename + full content]

  VARIANT B (adversarial revision):
  [For each file in B_FILES: show filename + full content]

  RULES:
  1. Output the COMPLETE synthesized file for each changed file
  2. Format: ### filename\n```\n{content}\n```
  3. Take the BEST approach from each variant for each section
  4. If A is better for some parts and B for others, merge them
  5. Do NOT explain — just output the files"

Capture AB-worker output → parse into `AB_FILES` map.

**Step 3: Shuffle and Label**

Randomly assign A, B, AB to labels X, Y, Z. Use:
```bash
LABELS=(X Y Z)
VARIANTS=(A B AB)
# Shuffle using shuf or a deterministic method:
SHUFFLED=$(printf '%s\n' A B AB | shuf)
```
Record the mapping (e.g. X→B, Y→AB, Z→A) in the autoreason log.

**Step 4: Spawn 2 Blind Judges** (Sonnet, parallel background)

Both judges get identical prompts with NO information about which variant is the incumbent:

Agent tool:
  model: "sonnet"
  run_in_background: true
  prompt: "You are a blind judge for the Autoreason protocol.
  You receive three implementation variants of a feature, labeled X, Y, Z.
  You do NOT know which is the original, the revision, or the synthesis.
  Rank them from best to worst.

  ACCEPTANCE CRITERIA:
  [list from prd.json]

  GATE RESULTS:
  [Gate Summary from VERIFY]

  VARIANT X:
  [Files for whichever variant maps to X]

  VARIANT Y:
  [Files for whichever variant maps to Y]

  VARIANT Z:
  [Files for whichever variant maps to Z]

  RANKING CRITERIA:
  1. Correctness: does it satisfy all acceptance criteria?
  2. Completeness: are edge cases handled?
  3. Code quality: clean, readable, follows patterns?
  4. Robustness: error handling, input validation?

  OUTPUT FORMAT (strict — nothing else):
  1st: [X/Y/Z]
  2nd: [X/Y/Z]
  3rd: [X/Y/Z]"

Wait for both judges. If one fails, accept single-judge ranking.

**Step 5: Tally Borda Scores**

Scoring: 1st = 3 pts, 2nd = 2 pts, 3rd = 1 pt
Sum across both judges (max 6 pts per variant).
Unmap X/Y/Z → A/B/AB using the shuffle mapping from Step 3.
Tiebreak order: AB > A > B

**Step 6: Decision**

- If A wins:
  - Increment `consecutive_a_wins`
  - If `consecutive_a_wins >= 2`: APPROVED — implementation is good enough. Break loop.
  - Otherwise: continue to next round
- If B or AB wins:
  - Reset `consecutive_a_wins = 0`
  - Apply winner's files to worktree: for each file in winner_FILES, Write to `{worktreePath}/{filename}`
  - Winner becomes new A: `A_FILES = winner_FILES`, re-capture `A_DIFF`
  - Continue to next round

**Step 7: Log Round**

Append to `tasks/ralph-autoreason-[story-id].md`:
```
## Round [N]
- Shuffle: A→[label], B→[label], AB→[label]
- Judge 1: 1st=[label] 2nd=[label] 3rd=[label]
- Judge 2: 1st=[label] 2nd=[label] 3rd=[label]
- Borda: A=[pts] B=[pts] AB=[pts]
- Winner: [A/B/AB]
- Action: [no change / applied B / applied AB]
```

End of loop body.

### 6c. Post-Autoreason

After loop exits (A wins twice, max 3 rounds, or timeout fallback):
- Log final verdict to autoreason log: `## Final: [winner] after [N] rounds`
- Update ralph-state.json: `autoReasonRound`, `consecutiveAWins`
- Proceed to phase: "HARDEN"

The existing REVIEW→IMPLEMENT retry loop is REMOVED. Autoreason replaces it internally
(B is the "fix" variant, AB is the "synthesis" — no separate fix worker needed).

### 6d. Fallback: Single-Reviewer Mode (preserved for timeout)

[Keep the FULL text of the original single-reviewer prompt here, indented or fenced,
so the coordinator can use it as the fallback when Autoreason times out. This is the
original Phase 6 Sonnet prompt from the pre-Autoreason version.]
```

### Edit 4: State reset in COMMIT phase (lines 700-707)
Add `"autoReasonRound": 0, "consecutiveAWins": 0` to the reset JSON.

### Edit 5: Recovery table (line 796)
Change REVIEW row from:
`| REVIEW | 3 total | phaseRetries.REVIEW | Proceed to HARDEN with logged items |`
To:
`| REVIEW | 3 Borda rounds | autoReasonRound | A wins 2x or max rounds → proceed to HARDEN |`

### Edit 6: Total fix workers (line 799)
Update calculation: Autoreason spawns max 4 agents/round (B + AB + 2 judges) × 3 rounds = 12, plus existing VERIFY (12) + HARDEN (2) = 26 worst case.

### Edit 7: Rule 8 (line 837)
Change from: "Cross-model review. Reviewer uses Sonnet (different model catches different bugs)."
To: "Autoreason review. A/B/AB blind Borda with Sonnet variants + Sonnet judges (NousResearch/autoreason)."

## Patterns to Follow

- Agent spawning: same format as existing IMPLEMENT/SCOUT sections (model, prompt, foreground/background)
- Phase transition: `Update state to \`phase: "HARDEN"\`.` + `---` rule
- State JSON: add fields alongside existing ones, don't restructure
- Progress log format: `[ISO-8601Z] RALPH | [story] | [FROM] → [TO]`
- printf '%s\n' for log appends

## Verification Gates (6 gates)

| Gate | Pass Condition |
|------|----------------|
| phase-6-exists | `## Phase 6: REVIEW` header present with Autoreason mention |
| three-variants | A/B/AB variant production described with agent prompts |
| blind-judging | Two judge agents with shuffle + no incumbent info |
| borda-scoring | 1st=3/2nd=2/3rd=1, tiebreak documented |
| convergence | A-wins-twice stop + max-3-rounds cap |
| fallback | 5-minute timeout check + single-reviewer fallback preserved |
| logging | tasks/ralph-autoreason-[story-id].md creation and per-round logging |
| state-fields | autoReasonRound + consecutiveAWins in state JSON example and reset |

## Gotchas

- The original single-reviewer prompt MUST be preserved as the timeout fallback (AC-8)
- B and AB workers are READ-ONLY — they output revised files as text, coordinator applies
- Judge prompts must NOT contain the words "original", "incumbent", "unchanged" for variants
- Shuffle must be truly random per round — don't reuse same mapping
- The REVIEW→IMPLEMENT retry loop (lines 561-568) is REMOVED — Autoreason replaces it
- Rule 5 "Reviewers are read-only" still applies — B/AB workers are producers, not reviewers

## Out of Scope

- No changes to other phases (LOAD, SCOUT, PLAN, IMPLEMENT, VERIFY, HARDEN, COMMIT)
- No changes to Phase 6.5 (HARDEN)
- No new files created (autoreason log is runtime-generated)
- No changes to Optimization Mode
