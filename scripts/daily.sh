#!/usr/bin/env bash
# Run the daily crawl on this machine (for cron or launchd). Extra arguments go to `ai-radar daily`.
#
#   scripts/daily.sh                      crawl and write today's digest, archive and feed
#   AI_RADAR_COMMIT=1 scripts/daily.sh    ...and also commit and push them
set -euo pipefail

cd "$(dirname "$0")/.."

# cron and launchd start with a minimal PATH; make sure `uv` can be found.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Optional settings live in .env (git-ignored), for example:
#   GITHUB_TOKEN=...      lifts the repo-search limit from 10 to 30 requests/minute
#   AI_RADAR_COMMIT=1     commit and push the results after each run
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

status=0
uv run ai-radar daily "$@" || status=$?

if [ "${AI_RADAR_COMMIT:-0}" = "1" ]; then
  git add data/items digests feeds
  if ! git diff --cached --quiet; then
    git commit -m "digest: $(date -u +%F)"
    git push
  fi
fi

exit "$status"
