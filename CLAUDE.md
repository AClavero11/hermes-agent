# Claude Code Addendum

Read AGENTS.md first. It contains the canonical Advanced Aerospace Operating Doctrine that governs all engineering decisions in this repo.

## Hermes-Specific Rules

Use plan mode before changing quote logic, schema, pricing rules, or external marketplace adapters.

For large procedures, create skills instead of expanding this file.

Prefer small, test-backed changes over broad refactors.

## SDA-WIRE Context (2026-04-10)

Branch `ralph/sda-wire` contains 6 signed commits (SWA-001 through SWA-006) wiring the Summit Domain Addon tools into the live auto-quote pipeline. This work is the extraction target for the canonical pricing engine described in AGENTS.md. See `docs/sda-wire-runbook.md` for the operational reference and `tasks/prd-sda-wire.json` for the PRD.

The SDA-WIRE bridge (`tools/auto_quote_bridge.py`) will become the Hermes adapter when the `quote-engine/` repo is stood up. Do not duplicate its logic — extract it.

## Testing

```bash
# Default (skips integration + domain_rules)
pytest

# Full SDA + SWA regression (198 tests)
pytest tests/tools/ tests/integration/ -m 'not integration or integration' -q

# Domain rules only
pytest -m domain_rules
```
