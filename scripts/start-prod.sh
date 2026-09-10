#!/bin/bash
#
# LangChain RAG Demo - Production Environment Startup Script
# 
# Usage:
#   ./scripts/start-prod.sh              # Start with health check waiting
#   SKIP_WAIT=1 ./scripts/start-prod.sh  # Skip health check waiting
#   FORCE_START=1 ./scripts/start-prod.sh # Skip confirmation prompt
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
ENV_FILE="$SCRIPT_DIR/../.env.prod"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.yml"
COMPOSE_ARGS=(-f "$COMPOSE_FILE" --env-file "$ENV_FILE")
ENV_NAME="PRODUCTION"
MAX_WAIT_SECONDS=${MAX_WAIT_SECONDS:-180}
WAIT_INTERVAL=${WAIT_INTERVAL:-5}

# Required variables for production
REQUIRED_VARS=(
    "POSTGRES_PASSWORD"
    "MINIO_ROOT_PASSWORD"
    "MINIO_SECRET_KEY"
    "GF_SECURITY_ADMIN_PASSWORD"
    "SECRET_KEY"
)

# ==============================================================================
# Banner
# ==============================================================================

print_banner() {
    local current_time
    current_time=$(date '+%Y-%m-%d %H:%M:%S')
    
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  LangChain RAG Demo 启动脚本${NC}"
    echo -e "${BOLD}  环境: ${RED}${ENV_NAME}${NC}"
    echo -e "${BOLD}  时间: ${current_time}${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo -e "${YELLOW}⚠ 警告: 这是生产环境启动脚本${NC}"
    echo -e "${YELLOW}⚠ 请确保已正确配置所有安全相关参数${NC}"
    echo ""
}

# ==============================================================================
# Confirmation Prompt
# ==============================================================================

confirm_start() {
    # Skip if FORCE_START is set
    if [[ "${FORCE_START:-0}" == "1" ]]; then
        log_info "已跳过确认提示 (FORCE_START=1)"
        return 0
    fi
    
    echo -e "${YELLOW}请确认以下事项:${NC}"
    echo "  1. .env.prod 文件已正确配置"
    echo "  2. 所有密码已修改为强密码"
    echo "  3. 数据备份已完成"
    echo ""
    
    read -p "$(echo -e "${BOLD}是否继续? [y/N]: ${NC}")" -r response
    echo ""
    
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        log_info "已取消启动"
        exit 0
    fi
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
    log_step "1/7" "检测运行环境..."
    
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
    
    # Check .env.prod exists
    if [[ -f "$ENV_FILE" ]]; then
        log_ok "配置文件 $ENV_FILE 存在"
    else
        log_error "配置文件 $ENV_FILE 不存在"
        log_info "请创建 .env.prod 文件，必须包含以下安全变量:"
        for var in "${REQUIRED_VARS[@]}"; do
            echo "  - $var"
        done
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
# Security Checks
# ==============================================================================

run_security_checks() {
    log_step "2/7" "执行安全检查..."
    
    # Temporarily load env file to check variables
    set -a
    source "$ENV_FILE"
    set +a
    
    local has_issues=false
    
    for var in "${REQUIRED_VARS[@]}"; do
        local value="${!var:-}"
        if [[ -z "$value" ]]; then
            log_error "变量 $var 未设置或为空"
            has_issues=true
        elif [[ "$value" == "admin" ]] || [[ "$value" == "password" ]] || [[ "$value" == "changeme" ]]; then
            log_warn "变量 $var 使用了不安全的默认值"
            has_issues=true
        else
            log_ok "变量 $var 已正确设置"
        fi
    done
    
    if [[ "$has_issues" == true ]]; then
        echo ""
        log_error "安全检查未通过，请修正上述问题后重试"
        exit 1
    fi
    
    echo ""
}

# ==============================================================================
# Load Environment Variables
# ==============================================================================

load_env_vars() {
    log_step "3/7" "加载环境变量..."

    # Source the .env.prod file（用于脚本内展示；compose 经 --env-file 自行读取同一文件）
    set -a
    source "$ENV_FILE"
    set +a

    log_ok "环境变量已加载"
    echo ""
}

# ==============================================================================
# Start Docker Compose Services
# ==============================================================================

start_services() {
    log_step "4/7" "启动 Docker Compose 服务..."
    log_info "拉取/构建镜像中，请耐心等待..."
    echo ""

    if $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" up -d --build; then
        echo ""
        log_ok "服务启动命令执行成功"
    else
        log_error "服务启动失败"
        log_info "请检查 Docker 日志: $DOCKER_COMPOSE_CMD ${COMPOSE_ARGS[*]} logs"
        exit 1
    fi

    echo ""
}

# ==============================================================================
# Health Check
# ==============================================================================

wait_for_health() {
    log_step "5/7" "等待服务健康检查..."
    
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
        status_output=$($DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" ps --format "table {{.Name}}\t{{.State}}\t{{.Status}}" 2>/dev/null || echo "")
        
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
        echo "    $DOCKER_COMPOSE_CMD ${COMPOSE_ARGS[*]} ps"
    fi

    echo ""

    # Print container status table
    print_container_status

    # Return status code
    [[ "$all_healthy" == true ]] && return 0 || return 1
}

# ==============================================================================
# Database Migration (Alembic)
# ==============================================================================

run_database_migration() {
    log_step "6/7" "执行数据库迁移（alembic upgrade head）..."

    # 容器内 POSTGRES_HOST=postgres 直连数据库（alembic 为主依赖，prod 镜像可用）
    if ! $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" exec -T backend alembic upgrade head; then
        # 全新数据库：backend 启动时 init_db(create_all) 已建全量表但无 alembic_version，
        # upgrade 撞 DuplicateTableError；此时 schema 与当前镜像模型一致，stamp 对齐即可
        log_warn "upgrade 未执行，尝试按全新库对齐 alembic 版本（stamp head）..."
        if ! $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" exec -T backend alembic stamp head; then
            log_error "数据库迁移失败，请检查 backend 容器日志"
            log_info "查看日志: $DOCKER_COMPOSE_CMD ${COMPOSE_ARGS[*]} logs backend"
            exit 1
        fi
        log_ok "已对齐 alembic 版本（全新库由启动建表）"
    else
        log_ok "数据库迁移完成"
    fi
    echo ""
}

print_container_status() {
    log_info "容器状态:"
    echo ""

    $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" ps --format "table {{.Name}}\t{{.State}}\t{{.Status}}" 2>/dev/null || \
    $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" ps

    echo ""
}

# ==============================================================================
# Print Access Information
# ==============================================================================

print_access_info() {
    log_step "7/7" "输出访问信息..."
    echo ""
    
    # In production, only show variable names, not actual values
    local pg_db="${POSTGRES_DB:-rag_demo}"
    local pg_user="${POSTGRES_USER:-postgres}"
    
    echo -e "${BOLD}========================================${NC}"
    echo -e "${BOLD}  访问地址汇总${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo ""
    echo -e "  ${CYAN}前端 & API${NC}"
    echo "    前端页面     : http://localhost:80"
    echo "    Backend API  : http://localhost:8001（仅回环绑定）"
    echo "    API 文档     : http://localhost:8001/docs"
    echo ""
    echo -e "  ${CYAN}存储服务${NC}"
    echo "    PostgreSQL   : localhost:5434"
    echo "                  数据库: $pg_db"
    echo "                  用户名: $pg_user"
    echo "                  密码  : \${POSTGRES_PASSWORD}"
    echo "    MinIO 控制台 : http://localhost:9003"
    echo "                  用户名: \${MINIO_ROOT_USER}"
    echo "                  密码  : \${MINIO_ROOT_PASSWORD}"
    echo "    Milvus       : localhost:19531"
    echo ""
    echo -e "  ${CYAN}监控服务${NC}"
    echo "    Prometheus   : http://localhost:9094"
    echo "    Grafana      : http://localhost:3001"
    echo "                  用户名: admin"
    echo "                  密码  : \${GF_SECURITY_ADMIN_PASSWORD}"
    echo "    Alertmanager : http://localhost:9095"
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "  ${YELLOW}常用命令${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo "    查看日志  : ./scripts/logs-prod.sh"
    echo "    停止服务  : ./scripts/stop-prod.sh"
    echo "    查看状态  : $DOCKER_COMPOSE_CMD ${COMPOSE_ARGS[*]} ps"
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
    confirm_start
    run_preflight_checks
    run_security_checks
    load_env_vars
    start_services
    wait_for_health
    local health_result=$?
    run_database_migration
    print_access_info
    
    if [[ $health_result -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}✓ 生产环境启动完成！${NC}"
    else
        echo -e "${YELLOW}${BOLD}! 生产环境已启动，但部分服务未完全就绪${NC}"
        echo -e "${YELLOW}${BOLD}! 请检查容器状态并确保所有服务正常运行${NC}"
        exit 1
    fi
    echo ""
}

# Run main function
main "$@"
