# AAC Local DeepSeek Profile

## Summary

Isolated local-only Hermes profile for the clean upstream `v2026.4.23` workspace:

- `HERMES_HOME`: `/Users/ac/.hermes-deepseek`
- Launch wrapper: `/Users/ac/.hermes-deepseek/bin/hermes`
- Health check: `/Users/ac/.hermes-deepseek/bin/deepseek-health`
- Default Studio endpoint: `http://100.84.138.43:8090/v1`

## What This Profile Does

- Keeps AAC memory and MCP context:
  - `SOUL.md`
  - `memories/MEMORY.md`
  - `memories/USER.md`
  - `mcp_servers/alexandria_server.py`
  - `mcp_servers/v11_server.py`
- Pins the main runtime to `custom:office-deepseek`
- Clears cloud LLM credentials from the runtime environment
- Disables AAC fork-specific heartbeat and batch jobs that do not exist in clean upstream

## Model Resolution

The wrapper resolves the model id in this order:

1. `DEEPSEEK_LOCAL_MODEL`
2. First model returned by `${DEEPSEEK_LOCAL_BASE_URL}/models` whose id contains `deepseek`
3. Fallback literal: `mlx-community/deepseek-ai-DeepSeek-V4-Flash-4bit`

This avoids accidentally selecting cached Qwen models from `mlx_lm.server` while still pinning the profile to a verified DeepSeek V4 MLX repo.

## Server Contract

The profile expects an OpenAI-compatible endpoint on the office Mac Studio. `mlx_lm.server` is the intended backend because prior AAC testing already proved that path on the Studio for Qwen.

Verified local target:

- Repo: `mlx-community/deepseek-ai-DeepSeek-V4-Flash-4bit`
- Disk footprint: `160 GB`
- Peak RAM target: `160 GB+`

Current upstream caveat:

- The model card states that DeepSeek V4 is not yet supported by stock `mlx-lm`.
- The Studio currently has `mlx-lm v0.31.2`, so DeepSeek V4 requires the temporary V4 branch:
  `https://github.com/machiabeli/mlx-lm-1` at `feat/deepseek-v4`.

Template commands on the Studio:

```bash
git clone https://github.com/machiabeli/mlx-lm-1.git ~/mlx-lm-v4
cd ~/mlx-lm-v4
git checkout feat/deepseek-v4
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -e .
pkill -f 'mlx_lm.server.*8090' 2>/dev/null || true
nohup .venv/bin/mlx_lm.server \
  --host 100.84.138.43 \
  --port 8090 \
  --model mlx-community/deepseek-ai-DeepSeek-V4-Flash-4bit \
  > /tmp/deepseek_v4_flash_4bit.log 2>&1 &
```

Once the V4 support branch lands in upstream `mlx-lm`, replace the temporary fork install with the stock package.

## Validation

Use:

```bash
/Users/ac/.hermes-deepseek/bin/deepseek-health
```

That checks:

1. `/v1/models`
2. A one-shot `/chat/completions` probe

## Current Studio State

As of the latest live check:

- The legacy Studio path already has a Python process history on `100.84.138.43:8080`
- `/v1/models` currently lists cached Qwen models, not DeepSeek
- `mlx_lm.server` is installed, but the stock `v0.31.2` build still needs the V4 branch for DeepSeek
- The isolated DeepSeek profile now uses dedicated port `8090` so it does not collide with the legacy `8080` server path
- The `mlx-community/DeepSeek-V4-Flash-4bit` repo now appears to contain both `33`- and `36`-shard sets, so the profile targets the cleaner `mlx-community/deepseek-ai-DeepSeek-V4-Flash-4bit` repo instead
