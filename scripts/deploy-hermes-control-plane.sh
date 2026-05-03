#!/usr/bin/env bash
set -euo pipefail

ROOT="${HERMES_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STUDIO_HOST="${HERMES_STUDIO_HOST:-studio}"
STUDIO_VERSIONED_TARGET="${HERMES_STUDIO_VERSIONED_TARGET:-/Users/anthonyclavero/.hermes/hermes-agent-v2026.4.23}"
STUDIO_ACTIVE_TARGET="${HERMES_STUDIO_ACTIVE_TARGET:-/Users/anthonyclavero/.hermes/hermes-agent}"
STUDIO_SERVICES_TARGET="${HERMES_STUDIO_SERVICES_TARGET:-/Users/anthonyclavero/.hermes/services}"
STUDIO_DEEPSEEK_BIN_TARGET="${HERMES_STUDIO_DEEPSEEK_BIN_TARGET:-/Users/anthonyclavero/.hermes-deepseek/bin}"
SSH_PROXYCOMMAND="${HERMES_STUDIO_PROXYCOMMAND:-nc -x localhost:1055 %h %p}"
RSYNC_RSH="${HERMES_STUDIO_RSYNC_RSH:-ssh -o ProxyCommand='nc -x localhost:1055 %h %p'}"

SSH_CMD=(ssh -o "ProxyCommand=${SSH_PROXYCOMMAND}")
if [[ "${HERMES_STUDIO_NO_PROXY:-}" == "1" ]]; then
  SSH_CMD=(ssh)
  RSYNC_RSH="${HERMES_STUDIO_RSYNC_RSH:-ssh}"
fi

REPO_FILES=(
  agent/prompt_builder.py
  gateway/context_router.py
  gateway/platforms/api_server.py
  gateway/platforms/base.py
  gateway/platforms/telegram.py
  gateway/run.py
  hermes_cli/canary.py
  hermes_cli/commands.py
  hermes_cli/goals.py
  hermes_cli/main.py
  hermes_cli/runtime_status.py
  hermes_cli/workspace.py
  run_agent.py
  toolsets.py
  tools/workspace_tool.py
  tools/x_scraper_tool.py
  tests/gateway/test_api_server.py
  tests/gateway/test_status_command.py
  tests/gateway/test_telegram_e2e_ack.py
  tests/hermes_cli/test_canary.py
  tests/tools/test_x_scraper_tool.py
  docs/AAC_LOCAL_DEEPSEEK.md
  docs/AAC_MIGRATION_v2026.4.23.md
  docs/HERMES_SELF_HEAL_PLAYBOOKS.md
  docs/patch-manifests/hermes-control-plane-2026-05-02.md
  scripts/hermes-canary-daily
  scripts/deploy-hermes-control-plane.sh
)

SERVICE_FILES=(
  /Users/ac/.hermes/services/service_env.py
  /Users/ac/.hermes/services/ils_auto_quote.py
  /Users/ac/.hermes/services/market_scanner.py
  /Users/ac/.hermes/services/quote_approval_bot.py
  /Users/ac/.hermes/services/quote_notify.py
  /Users/ac/.hermes/services/quote_pdf.py
  /Users/ac/.hermes/services/quote_pdf_server.py
  /Users/ac/.hermes/services/shipment_watcher.py
)

WRAPPER_FILE="${HERMES_DEEPSEEK_ENV_WRAPPER:-/Users/ac/.hermes-deepseek/bin/hermes-env.sh}"
CANARY_DAILY_FILE="${HERMES_DEEPSEEK_CANARY_DAILY:-${ROOT}/scripts/hermes-canary-daily}"
RUNTIME_VERSION_FILE="$(mktemp)"
trap 'rm -f "${RUNTIME_VERSION_FILE}"' EXIT

missing=0
for file in "${REPO_FILES[@]}"; do
  if [[ ! -f "${ROOT}/${file}" ]]; then
    echo "missing repo file: ${file}" >&2
    missing=1
  fi
done
for file in "${SERVICE_FILES[@]}" "${WRAPPER_FILE}" "${CANARY_DAILY_FILE}"; do
  if [[ ! -f "${file}" ]]; then
    echo "missing runtime file: ${file}" >&2
    missing=1
  fi
done
if [[ "${missing}" != "0" ]]; then
  exit 1
fi

SOURCE_SHA="$(git -C "${ROOT}" rev-parse HEAD 2>/dev/null || true)"
SOURCE_SHORT_SHA="$(git -C "${ROOT}" rev-parse --short=12 HEAD 2>/dev/null || true)"
SOURCE_BRANCH="$(git -C "${ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
SOURCE_DIRTY_COUNT="$(git -C "${ROOT}" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
SOURCE_DIRTY=false
if [[ "${SOURCE_DIRTY_COUNT}" != "0" ]]; then
  SOURCE_DIRTY=true
fi
DEPLOYED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
cat > "${RUNTIME_VERSION_FILE}" <<JSON
{
  "branch": "${SOURCE_BRANCH}",
  "deployed_at": "${DEPLOYED_AT}",
  "dirty": ${SOURCE_DIRTY},
  "dirty_count": ${SOURCE_DIRTY_COUNT},
  "manifest": "docs/patch-manifests/hermes-control-plane-2026-05-02.md",
  "sha": "${SOURCE_SHA}",
  "short_sha": "${SOURCE_SHORT_SHA}"
}
JSON

deploy_repo_target() {
  local target="$1"
  "${SSH_CMD[@]}" "${STUDIO_HOST}" "mkdir -p '${target}'"
  (
    cd "${ROOT}"
    rsync -aR -e "${RSYNC_RSH}" "${REPO_FILES[@]}" "${STUDIO_HOST}:${target}/"
  )
  rsync -a -e "${RSYNC_RSH}" "${RUNTIME_VERSION_FILE}" "${STUDIO_HOST}:${target}/.hermes-runtime-version.json"
}

deploy_repo_target "${STUDIO_VERSIONED_TARGET}"
deploy_repo_target "${STUDIO_ACTIVE_TARGET}"

"${SSH_CMD[@]}" "${STUDIO_HOST}" "mkdir -p '${STUDIO_SERVICES_TARGET}' '${STUDIO_DEEPSEEK_BIN_TARGET}'"
rsync -a -e "${RSYNC_RSH}" "${SERVICE_FILES[@]}" "${STUDIO_HOST}:${STUDIO_SERVICES_TARGET}/"
rsync -a -e "${RSYNC_RSH}" "${WRAPPER_FILE}" "${STUDIO_HOST}:${STUDIO_DEEPSEEK_BIN_TARGET}/hermes-env.sh"
rsync -a -e "${RSYNC_RSH}" "${CANARY_DAILY_FILE}" "${STUDIO_HOST}:${STUDIO_DEEPSEEK_BIN_TARGET}/hermes-canary-daily"

echo "deployed Hermes control-plane patch stack to ${STUDIO_HOST}"
