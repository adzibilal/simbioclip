#!/usr/bin/env bash
# SimbioClip helper — build / start / stop / debug the stack.
#
# Usage:
#   ./run.sh             # build + up (default)
#   ./run.sh up          # same as default
#   ./run.sh down        # stop containers
#   ./run.sh restart     # restart api + worker (no rebuild)
#   ./run.sh logs        # tail all logs
#   ./run.sh logs api    # tail one service
#   ./run.sh status      # ps
#   ./run.sh rebuild     # full rebuild (--no-cache) and up
#   ./run.sh shell       # bash into worker
#   ./run.sh shell api   # bash into api
#   ./run.sh clean       # down + remove images + named volumes

set -euo pipefail

cd "$(dirname "$0")"

# BuildKit + .dockerignore + cache mounts make rebuilds fast.
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# Pretty status helpers
say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }

# Ensure an .env exists so docker compose doesn't substitute empty strings silently.
if [[ ! -f .env ]]; then
  warn ".env not found — secrets will be empty. Copy .env.example if you have one."
fi

# Ensure third-party GPG keys are available before building (Docker's network
# often cannot reach deb.nodesource.com / dl.google.com during builds).
fetch_keys() {
  local dir="keys"
  mkdir -p "$dir"
  if [[ ! -f "$dir/nodesource.gpg" ]]; then
    say "Downloading NodeSource GPG key…"
    curl -fsSL --retry 3 --retry-delay 5 \
      https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key |
      gpg --dearmor -o "$dir/nodesource.gpg"
  fi
  if [[ ! -f "$dir/google-chrome.gpg" ]]; then
    say "Downloading Google Chrome GPG key…"
    curl -fsSL --retry 3 --retry-delay 5 \
      https://dl.google.com/linux/linux_signing_key.pub |
      gpg --dearmor -o "$dir/google-chrome.gpg"
  fi
}

# Ensure cookies.txt is a file, not a directory (common mistake that breaks
# Docker volume-mount — the container sees a dir and errors with "Is a directory").
if [[ -d cookies.txt ]]; then
  warn "cookies.txt is a directory — replacing with empty file."
  rm -rf cookies.txt && touch cookies.txt
fi
if [[ ! -f cookies.txt ]]; then
  touch cookies.txt
fi

cmd="${1:-up}"
shift || true

case "$cmd" in
  up|start)
    fetch_keys
    say "Building images…"
    docker compose build
    say "Starting containers…"
    docker compose up -d
    ok "Stack up."
    echo "    API     → http://localhost:8000"
    echo "    Logs    → ./run.sh logs"
    echo "    Status  → ./run.sh status"
    ;;

  down|stop)
    say "Stopping containers…"
    docker compose down
    ok "Stack down."
    ;;

  restart)
    docker compose restart "${@:-api worker}"
    ok "Restarted: ${*:-api worker}"
    ;;

  logs)
    docker compose logs -f --tail=80 "$@"
    ;;

  status|ps)
    docker compose ps
    ;;

  rebuild)
    say "Stopping containers…"
    docker compose down
    say "Rebuilding images from scratch (--no-cache)…"
    docker compose build --no-cache
    say "Starting containers…"
    docker compose up -d
    ok "Rebuilt and started."
    ;;

  shell)
    svc="${1:-worker}"
    say "Opening shell in $svc…"
    docker compose exec "$svc" /bin/bash || docker compose exec "$svc" /bin/sh
    ;;

  clean)
    warn "This removes all SimbioClip containers, images, and the clip_data volume."
    read -p "Type 'yes' to confirm: " confirm
    if [[ "$confirm" == "yes" ]]; then
      docker compose down -v --rmi local
      ok "Cleaned."
    else
      echo "Aborted."
    fi
    ;;

  help|-h|--help)
    grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'
    ;;

  *)
    warn "Unknown command: $cmd"
    echo "Run './run.sh help' for usage."
    exit 1
    ;;
esac
