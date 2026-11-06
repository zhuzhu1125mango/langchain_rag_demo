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
ENV_FILE="$SCRIPT_DIR/../.env.dev"
COMPOSE_FILE="$SCRIPT_DIR/../docker-compose.dev.yml"
COMPOSE_ARGS=(-f "$COMPOSE_FILE" --env-file "$ENV_FILE")
ENV_NAME="DEVELOPMENT"
# 精简模式（P0-3）：默认只起核心 7 个服务，不含 prometheus/grafana/postgres-exporter。
# 需要监控栈：INCLUDE_MONITORING=1 ./scripts/start-dev.sh
PROFILE_ARGS=""
if [ "${INCLUDE_MONITORING:-0}" = "1" ]; then
    PROFILE_ARGS="--profile monitoring"
fi
# 默认 240s：干净机器首次启动要等镜像构建 + 模型预热，实测 backend 到 healthy 约 3~4 分钟。
# 原来 120s 会在「其实马上就绪」时提前报失败（服务随后自己起来了，但用户只看到红字）。
MAX_WAIT_SECONDS=${MAX_WAIT_SECONDS:-240}
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
    log_step "1/5" "检测运行环境..."
    
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
    if [[ -f "$ENV_FILE" ]]; then
        log_ok "配置文件 $ENV_FILE 存在"
    else
        # 首次运行自动生成：全新 clone 里没有 .env.dev（.gitignore 排除 .env*），
        # 而 README 承诺「10 分钟上手」——让用户自己去凑 189 个键，这个承诺就不成立。
        # 只有 dev 这么干：生产必须由人显式配置强密码，脚本不替它猜。
        EXAMPLE_FILE="$SCRIPT_DIR/../.env.example"
        if [[ ! -f "$EXAMPLE_FILE" ]]; then
            log_error "既没有 $ENV_FILE 也没有 .env.example，无法自动生成配置"
            exit 1
        fi
        cp "$EXAMPLE_FILE" "$ENV_FILE"

        # SECRET_KEY 必须生成：留空时签发 JWT 会抛 ValueError（auth.py），
        # 表现是「注册/登录直接 500」，而 .env.example 里它本来就是空值。
        SECRET=""
        if command -v python3 >/dev/null 2>&1; then
            SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
        elif command -v openssl >/dev/null 2>&1; then
            SECRET=$(openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-64)
        fi
        if [[ -n "$SECRET" ]]; then
            _tmp_env="$ENV_FILE.tmp.$$"
            awk -v s="$SECRET" 'BEGIN{done=0} /^SECRET_KEY=/{print "SECRET_KEY=" s; done=1; next} {print} END{if(!done) print "SECRET_KEY=" s}' \
                "$ENV_FILE" > "$_tmp_env" && mv "$_tmp_env" "$ENV_FILE"
            log_ok "已从 .env.example 生成 $ENV_FILE，并随机生成 SECRET_KEY"
        else
            log_warn "已从 .env.example 生成 $ENV_FILE，但未能生成 SECRET_KEY"
            log_warn "请手动设置 SECRET_KEY（留空会导致注册/登录失败）"
        fi
        log_info "如需改端口/密码，编辑 $ENV_FILE 后重跑本脚本"
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
# Load Environment Variables
# ==============================================================================

# 展示用非敏感键（W6 #76：不再 source 整个 env 文件——凭据不进脚本环境/不导出
# 给子进程；compose 经 --env-file 自行读取同一文件，无需 shell 导出）
DEV_DISPLAY_KEYS=(POSTGRES_DB POSTGRES_USER MINIO_ROOT_USER)

load_env_vars() {
    log_step "2/5" "加载环境变量..."

    for key in "${DEV_DISPLAY_KEYS[@]}"; do
        local value
        value=$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '"')
        if [[ -n "$value" ]]; then
            declare -g "$key=$value"
        fi
    done

    log_ok "环境变量已加载（仅非敏感展示键）"
    echo ""
}

# ==============================================================================
# Start Docker Compose Services
# ==============================================================================

start_services() {
    log_step "3/5" "启动 Docker Compose 服务..."

    # CHECK_ONLY=1：只跑预检（配置文件、compose 解析、服务清单），不起容器。
    # 用于「别人能不能跑起来」的复现验证与 CI——否则验证这一步要真拉一遍镜像。
    if [[ "${CHECK_ONLY:-0}" == "1" ]]; then
        log_info "CHECK_ONLY=1，仅校验配置，不启动容器"
        if $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" $PROFILE_ARGS config --services > /tmp/_compose_services.txt 2>/tmp/_compose_err.txt; then
            local count
            count=$(grep -c . /tmp/_compose_services.txt)
            log_ok "compose 解析通过，服务数 $count："
            sed 's/^/    - /' /tmp/_compose_services.txt
            rm -f /tmp/_compose_services.txt /tmp/_compose_err.txt
            exit 0
        else
            log_error "compose 解析失败："
            sed 's/^/    /' /tmp/_compose_err.txt
            rm -f /tmp/_compose_services.txt /tmp/_compose_err.txt
            exit 1
        fi
    fi

    log_info "拉取/构建镜像中，请耐心等待..."
    echo ""

    if $DOCKER_COMPOSE_CMD "${COMPOSE_ARGS[@]}" $PROFILE_ARGS up -d --build; then
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
    log_step "4/5" "等待服务健康检查..."
    
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
    
    echo ""
    
    # Return status code
    [[ "$all_healthy" == true ]] && return 0 || return 1
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
    log_step "5/5" "输出访问信息..."
    echo ""

    # Read env vars for display (dev mode shows actual values)
    local pg_db="${POSTGRES_DB:-rag_demo}"
    local pg_user="${POSTGRES_USER:-postgres}"
    local minio_user="${MINIO_ROOT_USER:-minioadmin}"
    
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
    echo "                  密码  : 见 .env.dev 的 GF_SECURITY_ADMIN_PASSWORD"
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo -e "  ${YELLOW}常用命令${NC}"
    echo -e "${BOLD}========================================${NC}"
    echo "    查看日志  : ./scripts/logs-dev.sh"
    echo "    停止服务  : ./scripts/stop-dev.sh"
    echo "    查看状态  : $DOCKER_COMPOSE_CMD ${COMPOSE_ARGS[*]} ps"
    echo "    进入容器  : docker exec -it <container_name> bash"
    echo ""
    echo -e "${BOLD}========================================${NC}"
    echo ""
}

# ==============================================================================
# Main
# ==============================================================================

check_ollama_models() {
    # Ollama 不在 compose 里（本机安装），后端启动时会校验模型是否存在，缺失即拒绝启动。
    # 不预先检查的话，用户看到的是「7 个容器起来了但 backend unhealthy」，
    # 而真实原因是模型没拉——这一步只是把那个报错提前成一句人话。
    local skip="${SKIP_OLLAMA_CHECK:-0}"
    local env_models=()

    # 只校验配置时不该要求本机装着 Ollama（CI / 复现验证都跑这个分支）
    if [[ "${CHECK_ONLY:-0}" == "1" ]]; then
        log_info "CHECK_ONLY=1，跳过 Ollama 模型检查"
        return 0
    fi

    local embedding_model direct_model think_model
    # 取值必须去掉行内注释：.env.example 里写的是 `OLLAMA_MODEL_NAME=qwen3:4b  # 说明`，
    # 直接 cut 会把注释一起当成值，结果是「模型明明装了却报缺失」。
    _env_value() {
        grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- \
            | sed 's/[[:space:]]*#.*$//' | tr -d '"' \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
    }
    embedding_model=$(_env_value EMBEDDING_MODEL_NAME)
    direct_model=$(_env_value OLLAMA_DIRECT_MODEL_NAME)
    think_model=$(_env_value OLLAMA_MODEL_NAME)
    for m in "$embedding_model" "$direct_model" "$think_model"; do
        [[ -n "$m" ]] && env_models+=("$m")
    done

    if [[ "$skip" == "1" ]]; then
        log_warn "SKIP_OLLAMA_CHECK=1，跳过模型检查（缺失将导致后端启动失败）"
        return 0
    fi

    if ! command -v ollama >/dev/null 2>&1; then
        log_error "未找到 ollama 命令：Ollama 不在 compose 内，需要单独安装并启动"
        log_info "安装：https://ollama.com/download"
        log_info "装好后拉取模型（见 README「上手」一节），或设 SKIP_OLLAMA_CHECK=1 跳过本检查"
        exit 1
    fi

    local tags
    if ! tags=$(ollama list 2>/dev/null | awk 'NR>1 {print $1}'); then
        log_error "无法读取 ollama 模型列表（Ollama 服务可能没启动）"
        log_info "先执行 ollama serve，或检查 Docker Desktop 内 Ollama 是否运行"
        exit 1
    fi

    local missing=()
    for want in "${env_models[@]}"; do
        local base="${want%%:*}"   # 忽略 tag 差异：bge-m3 与 bge-m3:latest 视为同一个
        if ! grep -qx -e "$want" -e "$base:latest" -e "$base" <<< "$tags"; then
            missing+=("$want")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "缺少 Ollama 模型：${missing[*]}"
        log_info "按下面三条补齐（约 5.6GB，首次下载较慢）："
        printf '    ollama pull %s\n' "${missing[@]}"
        log_info "只想先看配置对不对，可用 CHECK_ONLY=1 跳过启动"
        exit 1
    fi

    log_ok "Ollama 模型齐全：${env_models[*]}"
}

main() {
    print_banner
    run_preflight_checks
    load_env_vars
    check_ollama_models
    start_services
    # 抑制 set -e：健康检查未全部就绪时返回 1，不能提前退出，需让下方提示可达
    set +e
    wait_for_health
    health_result=$?
    set -e
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
