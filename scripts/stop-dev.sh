#!/bin/bash
#
# LangChain RAG Demo - Stop Development Environment
#
# Usage:
#   ./scripts/stop-dev.sh           # Stop services
#   ./scripts/stop-dev.sh --clean   # Stop and remove volumes
#

set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.dev.yml"

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
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED=''
    GREEN=''
    YELLOW=''
    CYAN=''
    BOLD=''
    NC=''
fi

echo ""
echo -e "${BOLD}========================================${NC}"
echo -e "${BOLD}  停止开发环境${NC}"
echo -e "${BOLD}========================================${NC}"
echo ""

if [[ "${1:-}" == "--clean" ]]; then
    echo -e "${YELLOW}警告: 将删除所有数据卷！${NC}"
    read -p "确认继续? [y/N]: " -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "已取消"
        exit 0
    fi
    echo ""
    echo "停止服务并删除数据卷..."
    $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" down -v
    echo ""
    echo -e "${GREEN}✓ 开发环境已停止，数据卷已删除${NC}"
else
    echo "停止服务..."
    $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" down
    echo ""
    echo -e "${GREEN}✓ 开发环境已停止${NC}"
    echo ""
    echo "提示: 使用 './scripts/stop-dev.sh --clean' 可同时删除数据卷"
fi

echo ""
