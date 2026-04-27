#!/usr/bin/env bash
set -euo pipefail

# Maxwell-Daemon Deployment Rollback Script
# Usage: ./rollback.sh [VERSION]
# If VERSION is not provided, rolls back to the previous deployment.

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${DEPLOY_DIR}/.."
VERSION="${1:-}"

log() {
    echo "[$(date -Iseconds)] $*"
}

if [[ -z "$VERSION" ]]; then
    if [[ ! -f "${APP_DIR}/.deployment_history" ]]; then
        log "ERROR: No deployment history found. Cannot determine previous version."
        exit 1
    fi
    # Get the second-to-last deployment (previous)
    VERSION=$(tail -n 2 "${APP_DIR}/.deployment_history" | head -n 1 | awk '{print $1}')
    if [[ -z "$VERSION" ]]; then
        log "ERROR: Could not determine previous version from history."
        exit 1
    fi
    log "Rolling back to previous version: $VERSION"
else
    log "Rolling back to specified version: $VERSION"
fi

# Validate version exists in history or is a known tag
if ! git -C "$APP_DIR" rev-parse "$VERSION" >/dev/null 2>&1; then
    log "ERROR: Version $VERSION is not a valid git reference."
    exit 1
fi

# Stop services
log "Stopping Maxwell-Daemon services..."
if systemctl is-active --quiet maxwell-daemon 2>/dev/null; then
    sudo systemctl stop maxwell-daemon
    log "Service stopped."
else
    log "Service not running (or not managed by systemd)."
fi

# Perform rollback
log "Checking out version $VERSION..."
git -C "$APP_DIR" checkout "$VERSION"
git -C "$APP_DIR" submodule update --init --recursive 2>/dev/null || true

# Reinstall dependencies
log "Reinstalling dependencies..."
if command -v uv >/dev/null 2>&1; then
    uv sync --all-extras
elif command -v pip >/dev/null 2>&1; then
    pip install -e "${APP_DIR}[all]"
else
    log "WARNING: No package manager found (uv or pip). Skipping dependency reinstall."
fi

# Restart services
log "Restarting Maxwell-Daemon services..."
if systemctl is-active --quiet maxwell-daemon 2>/dev/null || systemctl list-unit-files | grep -q maxwell-daemon; then
    sudo systemctl start maxwell-daemon
    log "Service restarted."
else
    log "WARNING: systemd service not found. Please start the daemon manually."
fi

# Health check
log "Running health check..."
for i in {1..30}; do
    if curl -sf http://localhost:8080/health >/dev/null 2>&1; then
        log "Health check passed. Rollback complete."
        exit 0
    fi
    sleep 1
done

log "ERROR: Health check failed after rollback. Manual intervention required."
exit 1