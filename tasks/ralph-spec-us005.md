# Implementation Spec: US-005 — ruff + vulture pre-commit dead code gate

## Objective

Install a pre-commit hook that runs ruff and vulture on staged Python files to block dead code from landing in the repo. Framework-aware whitelist prevents false positives from pytest, FastAPI, Pydantic, Telegram, and fire patterns.

## Acceptance Criteria

1. Pre-commit hook at `.git/hooks/pre-commit`
2. Runs `ruff check` on staged `.py` files only (not entire repo)
3. Runs `vulture` with `--min-confidence 80` on staged `.py` files
4. Framework-aware whitelist at `.vulture-whitelist.py`
5. Blocks commit if ruff errors or vulture finds dead code above threshold
6. Clear error output with `file:line`
7. <5 seconds for 10-20 staged files
8. `requirements-dev.txt` with pinned ruff + vulture versions
9. `--no-verify` bypass (standard git behavior)

## Files to Create

| File | Purpose |
|------|---------|
| `.git/hooks/pre-commit` | Executable shell script — the hook itself |
| `.vulture-whitelist.py` | Framework dummy usages to suppress false positives |
| `requirements-dev.txt` | Pinned ruff + vulture for reproducible installs |

## Files to Modify

| File | Change |
|------|--------|
| `pyproject.toml` | Add `vulture` to `[project.optional-dependencies].dev` |

## Pre-commit Hook Design (`.git/hooks/pre-commit`)

```bash
#!/usr/bin/env bash
# ruff + vulture pre-commit gate
# Runs on staged .py files only. Bypass: git commit --no-verify

set -euo pipefail

# Get staged .py files (Added, Copied, Modified — not Deleted)
STAGED_PY=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$' || true)

# Nothing to check
if [ -z "$STAGED_PY" ]; then
    exit 0
fi

EXIT_CODE=0

# --- ruff check (errors only, warnings allowed per AC) ---
echo "=== ruff check ==="
if ! echo "$STAGED_PY" | xargs ruff check --no-fix 2>&1; then
    EXIT_CODE=1
fi

# --- vulture (dead code detection) ---
echo "=== vulture ==="
WHITELIST=""
if [ -f .vulture-whitelist.py ]; then
    WHITELIST=".vulture-whitelist.py"
fi

# Exclude paths that vulture shouldn't scan even if staged
FILTERED_PY=$(echo "$STAGED_PY" | grep -vE '^(mini-swe-agent/|tinker-atropos/|\.venv/)' || true)

if [ -n "$FILTERED_PY" ]; then
    if ! echo "$FILTERED_PY" | xargs vulture --min-confidence 80 $WHITELIST 2>&1; then
        EXIT_CODE=1
    fi
fi

if [ "$EXIT_CODE" -ne 0 ]; then
    echo ""
    echo "Pre-commit failed. Fix errors above or bypass with: git commit --no-verify"
fi

exit $EXIT_CODE
```

Key decisions:
- `--diff-filter=ACM` excludes Deleted files (can't lint deleted files)
- `grep -vE` excludes mini-swe-agent/, tinker-atropos/, .venv/ from vulture
- ruff uses existing pyproject.toml config (line-length, select, ignore already set)
- `--no-fix` prevents ruff from auto-fixing during commit
- `$WHITELIST` variable handles missing whitelist gracefully
- `xargs` passes filenames as args — efficient, handles spaces via default IFS

## Vulture Whitelist Design (`.vulture-whitelist.py`)

Framework-aware whitelist for patterns vulture falsely flags:

```python
"""Vulture whitelist — suppress false positives from framework patterns."""

# pytest fixtures (resolved by name injection, not direct call)
pytest_fixture = None  # noqa: F841
fixture = None  # noqa: F841

# FastAPI / Pydantic model config
model_config = None  # noqa: F841
model_validator = None  # noqa: F841

# Telegram bot handlers (registered via app.add_handler)
start_command = None  # noqa: F841
help_command = None  # noqa: F841

# fire.Fire() entrypoints (called dynamically)
main = None  # noqa: F841

# Tool registry pattern (TOOL_NAME/TOOL_DESCRIPTION used by discovery)
TOOL_NAME = ""  # noqa: F841
TOOL_DESCRIPTION = ""  # noqa: F841
execute = None  # noqa: F841

# __all__ exports
__all__ = []  # noqa: F841

# Abstract method implementations
pass  # abstractmethod overrides detected by class hierarchy
```

Note: The actual whitelist will use vulture's expected format — function/variable/attribute dummy assignments that match the names vulture reports.

## requirements-dev.txt

```
ruff==0.11.6
vulture==2.14
```

Pin to current latest stable versions. Matches the ruff version in .pre-commit-config.yaml.

## pyproject.toml Change

Add `"vulture"` to the dev dependencies list:
```
dev = ["pytest", "pytest-asyncio", "ruff", "mypy", "vulture"]
```

## Verification Gates

| Gate | Pass Condition | Command |
|------|----------------|---------|
| hook-exists | `.git/hooks/pre-commit` exists + executable | `test -x .git/hooks/pre-commit` |
| hook-syntax | Script passes bash syntax check | `bash -n .git/hooks/pre-commit` |
| whitelist-exists | `.vulture-whitelist.py` exists | `test -f .vulture-whitelist.py` |
| whitelist-syntax | Valid Python | `python3 -c "compile(open('.vulture-whitelist.py').read(), '.vulture-whitelist.py', 'exec')"` |
| requirements-exists | `requirements-dev.txt` exists with ruff + vulture | `grep ruff requirements-dev.txt && grep vulture requirements-dev.txt` |
| pyproject-updated | vulture in dev deps | `grep vulture pyproject.toml` |
| ruff-staged-only | Hook uses `git diff --cached --name-only` | `grep 'diff --cached' .git/hooks/pre-commit` |
| vulture-confidence | Hook passes `--min-confidence 80` | `grep 'min-confidence 80' .git/hooks/pre-commit` |
| hook-blocks | Exit code 1 on errors | `grep 'EXIT_CODE=1' .git/hooks/pre-commit` |

## Gotchas

- `.git/hooks/pre-commit` must be chmod +x (executable)
- Hook runs in repo root — relative paths work
- ruff config from pyproject.toml auto-applies (no --config needed)
- vulture whitelist uses its own format (dummy assignments), not comments
- tests/ excluded from vulture via grep filter (too many fixtures = noise)
- xargs on macOS handles filenames differently — test with spaces in names
- AC-9: `--no-verify` is standard git, no custom code needed

## Out of Scope

- No CI changes (existing ruff step in tests.yml stays as-is)
- No changes to .pre-commit-config.yaml (can coexist)
- No ruff config changes (already well-configured in pyproject.toml)
