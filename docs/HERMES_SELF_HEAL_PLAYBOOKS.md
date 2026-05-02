# Hermes Self-Heal Playbooks

These are operator playbooks for Hermes gateway regressions. They are written for Studio Hermes with DeepSeek V4 as the local planner, OpenAI frontier as the stronger route when quota is available, Telegram as the live interface, and `hermes canary` as the metrics harness.

## Planner Route

- Confirm the active route with `hermes canary --env-wrapper ~/.hermes-deepseek/bin/hermes-env.sh`.
- Use `custom:openai-frontier` / `model_aliases.frontier` only when the env wrapper reports `HERMES_OPENAI_FRONTIER_AVAILABLE=1`.
- Otherwise use `custom:office-deepseek-v4` and compensate with Alexandria/V11 retrieval, small bounded plans, tests, and canary verification.
- For architecture, production, root-cause debugging, RFQ/quote, V11, finance, or customer-facing work, require source-grounded evidence before acting.

## Canary Regression

- Read `~/.hermes-deepseek/canary/reports/latest.json` first.
- If only `live.x_scrape` fails, rerun once before changing code because X mirrors can be transient.
- If `live.gateway_health` or `live.behavior_golden` fails, check `8643` health and the API key env resolution before model debugging.
- If a contract golden fails, fix the code path or golden expectation, then run focused tests before redeploying.

## Gateway Split-Brain

- Check listeners with `lsof -nP -iTCP:8642 -iTCP:8643 -sTCP:LISTEN`.
- Launchd must own the wrapper process, and the wrapper must start Hermes through `gateway run --replace`.
- If an orphaned `python -m gateway.run` survives without owning ports, terminate that stale PID after confirming the supervised PID owns both ports.
- Rerun the live canary after cleanup.

## Stuck Session

- Gateway restart drain timeouts should mark sessions `resume_pending` and interrupt active agents.
- After repeated restart-active loops, stuck-loop counters should suspend the session instead of blindly resuming.
- Prefer `/stop`, `/new`, or a bounded follow-up prompt over another unbounded continuation.

## Telegram/API Health

- `8642` is the org health API; `8643` is the OpenAI-compatible Hermes API.
- Telegram webhook mode should stay connected after restart; API health must remain available at `http://127.0.0.1:8643/health`.
- Exact diagnostic probes must bypass the LLM and return immediately on both `/v1/responses` and `/v1/chat/completions`.
- External sends and destructive shell actions remain approval-gated even during self-heal.
