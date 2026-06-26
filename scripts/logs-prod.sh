#!/bin/bash
#
# LangChain RAG Demo - View Production Logs
#
# Usage:
#   ./scripts/logs-prod.sh           # View all logs
#   ./scripts/logs-prod.sh backend   # View only backend logs
#   ./scripts/logs-prod.sh frontend  # View only frontend logs
#

set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.yml"

# Determine docker compose command
if docker compose version &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE_CMD="docker-compose"
else
    echo "Error: Docker Compose not found"
    exit 1
fi

# Color support
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    CYAN=''
    BOLD=''
    NC=''
fi

echo -e "${BOLD}${CYAN}=== Production Environment Logs ===${NC}"
echo ""

# If specific service is requested
if [[ -n "${1:-}" ]]; then
    echo "Viewing logs for: $1"
    echo ""
    $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail=200 "$1"
else
    echo "Viewing logs for all services (Ctrl+C to exit)"
    echo ""
    echo "Tip: Use './scripts/logs-prod.sh <service>' to view specific service logs"
    echo "     Available services: backend, frontend, postgres, minio, milvus-standalone, redis, searxng, prometheus, grafana, alertmanager"
    echo ""
    $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" logs -f --tail=100
fi
