#!/bin/bash
#
# LangChain RAG Demo - Development Environment Startup Script
# 
# Usage:
#   ./scripts/start-dev.sh              # Start with health check waiting
#   SKIP_WAIT=1 ./scripts/start-dev.sh  # Skip health check waiting
#

set -euo pipefail

# ==============================================================================
# Color Definitions & Logging Functions
# ==============================================================================

# Check if terminal supports colors
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m' # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    CYAN=''
    BOLD=''
    NC=''
fi

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()      { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()    { echo -e "${CYAN}[$1]${NC} $2"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_fail()    { echo -e "${RED}✗${NC} $1"; }

# ==============================================================================
# Configuration
# ==============================================================================

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
ENV_FILE_SOURCE="$SCRIPT_DIR/../.env.dev"
ENV_FILE_TARGET="$SCRIPT_DIR/../.env"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.dev.yml"
ENV_NAME="DEVELOPMENT"
MAX_WAIT_SECONDS=${MAX_WAIT_SECONDS:-120}
WAIT_INTERVAL=${WAIT_INTERVAL:-5}

# ==============================================================================
# Banner
# ==============================================================================

print_banner() {
    local current_time
    current_time=$(date '+%Y-%m-%d %H:%M:%S')
    
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  LangChain RAG Demo 启动脚本${NC}"
    echo -e "${BOLD}  环境: ${CYAN}${ENV_NAME}${NC}"
    echo -e "${BOLD}  时间: ${current_time}${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
}

# ==============================================================================
# Pre-flight Checks
# ==============================================================================

check_command() {
    local cmd="$1"
    local package="${2:-$1}"
    
    if command -v "$cmd" &> /dev/null; then
        return 0
    else
        return 1
    fi
}

run_preflight_checks() {
    log_step "1/6" "检测运行环境..."
    
    # Check docker
    if check_command "docker"; then
        local docker_version
        docker_version=$(docker --version 2>/dev/null | head -1 || echo "unknown")
        log_ok "Docker 已安装: $docker_version"
    else
        log_error "Docker 未安装，请先安装 Docker"
        exit 1
    fi
    
    # Check docker compose (support both 'docker compose' and 'docker-compose')
    if docker compose version &> /dev/null; then
        DOCKER_COMPOSE_CMD="docker compose"
        local compose_version
        compose_version=$(docker compose version 2>/dev/null | head -1 || echo "unknown")
        log_ok "Docker Compose 已安装: $compose_version"
    elif check_command "docker-compose"; then
        DOCKER_COMPOSE_CMD="docker-compose"
        local compose_version
        compose_version=$(docker-compose --version 2>/dev/null | head -1 || echo "unknown")
        log_ok "Docker Compose 已安装: $compose_version"
    else
        log_error "Docker Compose 未安装，请先安装 Docker Compose"
        exit 1
    fi
    
    # Check .env.dev exists
    if [[ -f "$ENV_FILE_SOURCE" ]]; then
        log_ok "配置文件 $ENV_FILE_SOURCE 存在"
    else
        log_error "配置文件 $ENV_FILE_SOURCE 不存在"
        log_info "请创建 .env.dev 文件，可参考以下必要变量:"
        echo "  POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD"
        echo "  MINIO_ROOT_USER, MINIO_ROOT_PASSWORD"
        echo "  MINIO_ACCESS_KEY, MINIO_SECRET_KEY"
        echo "  GF_SECURITY_ADMIN_PASSWORD"
        exit 1
    fi
    
    # Check docker daemon is running
    if docker info &> /dev/null; then
        log_ok "Docker 守护进程运行中"
    else
        log_error "Docker 守护进程未运行，请启动 Docker Desktop 或 Docker 服务"
        exit 1
    fi
    
    echo ""
}

# ==============================================================================
# Copy Environment File
# ==============================================================================

copy_env_file() {
    log_step "2/6" "复制配置文件..."
    
    # Backup existing .env if exists
    if [[ -f "$ENV_FILE_TARGET" ]]; then
        local backup_file="${ENV_FILE_TARGET}.backup.$(date +%Y%m%d%H%M%S)"
        cp "$ENV_FILE_TARGET" "$backup_file"
        log_info "已备份旧配置到 $backup_file"
    fi
    
    # Copy .env.dev to .env
    if cp "$ENV_FILE_SOURCE" "$ENV_FILE_TARGET"; then
        log_ok "已复制 $ENV_FILE_SOURCE -> $ENV_FILE_TARGET"
    else
        log_error "复制配置文件失败"
        exit 1
    fi
    
    echo ""
}

# ==============================================================================
# Load Environment Variables
# ==============================================================================

load_env_vars() {
    log_step "3/6" "加载环境变量..."
    
    # Source the .env file
    set -a
    source "$ENV_FILE_TARGET"
    set +a
    
    log_ok "环境变量已加载"
    echo ""
}

# ==============================================================================
# Start Docker Compose Services
# ==============================================================================

start_services() {
    log_step "4/6" "启动 Docker Compose 服务..."
    log_info "拉取/构建镜像中，请耐心等待..."
    echo ""
    
    if $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" up -d --build; then
        echo ""
        log_ok "服务启动命令执行成功"
    else
        log_error "服务启动失败"
        log_info "请检查 Docker 日志: $DOCKER_COMPOSE_CMD -f $COMPOSE_FILE logs"
        exit 1
    fi
    
    echo ""
}

# ==============================================================================
# Health Check
# ==============================================================================

wait_for_health() {
    log_step "5/6" "等待服务健康检查..."
    
    # Check if SKIP_WAIT is set
    if [[ "${SKIP_WAIT:-0}" == "1" ]]; then
        log_warn "已跳过健康检查等待 (SKIP_WAIT=1)"
        echo ""
        return 0
    fi
    
    local elapsed=0
    local all_healthy=false
    
    echo ""
    
    while [[ $elapsed -lt $MAX_WAIT_SECONDS ]]; do
        # Get container status
        local status_output
        status_output=$($DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.State}}\t{{.Status}}" 2>/dev/null || echo "")
        
        # Count containers and healthy ones
        local total_containers=0
        local healthy_containers=0
        local running_containers=0
        
        # Parse status
        while IFS=$'\t' read -r name state status; do
            [[ -z "$name" || "$name" == "NAME" ]] && continue
            ((total_containers++))
            
            if [[ "$state" == "running" ]]; then
                ((running_containers++))
                # Check if healthy (health check passed) or no health check configured
                if [[ "$status" == *"healthy"* ]] || [[ "$status" != *"health"* ]]; then
                    ((healthy_containers++))
                fi
            fi
        done <<< "$status_output"
        
        # Print current status
        echo -ne "\r\033[K"  # Clear line
        echo -ne "  等待中... ($elapsed/$MAX_WAIT_SECONDS 秒) - 健康: $healthy_containers/$total_containers"
        
        # Check if all are healthy
        if [[ $total_containers -gt 0 && $healthy_containers -eq $total_containers ]]; then
            all_healthy=true
            break
        fi
        
        sleep $WAIT_INTERVAL
        ((elapsed += WAIT_INTERVAL))
    done
    
    echo ""  # New line after progress
    
    if [[ "$all_healthy" == true ]]; then
        log_ok "所有服务已就绪"
    else
        log_warn "部分服务未在 ${MAX_WAIT_SECONDS} 秒内完全就绪"
        log_info "请使用以下命令查看详细状态:"
        echo "    $DOCKER_COMPOSE_CMD -f $COMPOSE_FILE ps"
    fi
    
    echo ""
    
    # Print container status table
    print_container_status
    
    # Return status code
    [[ "$all_healthy" == true ]] && return 0 || return 1
}

print_container_status() {
    log_info "容器状态:"
    echo ""
    
    $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.State}}\t{{.Status}}" 2>/dev/null || \
    $DOCKER_COMPOSE_CMD -f "$COMPOSE_FILE" ps
    
    echo ""
}

# ==============================================================================
# Print Access Information
# ==============================================================================

print_access_info() {
    log_step "6/6" "输出访问信息..."
    echo ""
    
    # Read env vars for display (dev mode shows actual values)
    local pg_db="${POSTGRES_DB:-rag_demo}"
    local pg_user="${POSTGRES_USER:-postgres}"
    local minio_user="${MINIO_ROOT_USER:-minioadmin}"
    local grafana_pwd="${GF_SECURITY_ADMIN_PASSWORD:-admin}"
    
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  访问地址汇总${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo -e "  ${CYAN}前端 & API${NC}"
    echo "    前端页面     : http://localhost:5173"
    echo "    Backend API  : http://localhost:8000"
    echo "    API 文档     : http://localhost:8000/docs"
    echo ""
    echo -e "  ${CYAN}存储服务${NC}"
    echo "    PostgreSQL   : localhost:5433"
    echo "                  数据库: $pg_db"
    echo "                  用户名: $pg_user"
    echo "    MinIO 控制台 : http://localhost:9001"
    echo "                  用户名: $minio_user"
    echo "    Milvus       : localhost:19530"
    echo ""
    echo -e "  ${CYAN}监控服务${NC}"
    echo "    Prometheus   : http://localhost:9090"
    echo "    Grafana      : http://localhost:3000"
    echo "                  用户名: admin"
    echo "                  密码  : $grafana_pwd"
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "  ${YELLOW}常用命令${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo "    查看日志  : ./scripts/logs-dev.sh"
    echo "    停止服务  : ./scripts/stop-dev.sh"
    echo "    查看状态  : $DOCKER_COMPOSE_CMD -f $COMPOSE_FILE ps"
    echo "    进入容器  : docker exec -it <container_name> bash"
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo ""
}

# ==============================================================================
# Main
# ==============================================================================

main() {
    print_banner
    run_preflight_checks
    copy_env_file
    load_env_vars
    start_services
    wait_for_health
    local health_result=$?
    print_access_info
    
    if [[ $health_result -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}✓ 开发环境启动完成！${NC}"
    else
        echo -e "${YELLOW}${BOLD}! 开发环境已启动，但部分服务未完全就绪${NC}"
        echo -e "${YELLOW}${BOLD}! 请检查容器状态并确保所有服务正常运行${NC}"
        exit 1
    fi
    echo ""
}

# Run main function
main "$@"
