#!/bin/bash
# =============================================================================
# PostgreSQL 初始化脚本：启用 pg_stat_statements 扩展
# -----------------------------------------------------------------------------
# 用途：在 PostgreSQL 首次启动时（docker-entrypoint-initdb.d 阶段）执行，
# 创建 pg_stat_statements 扩展，用于采集 SQL 查询统计信息，
# 供 postgres-exporter 暴露给 Prometheus 监控。
# =============================================================================
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;          -- 创建查询统计扩展
    GRANT SELECT ON pg_stat_statements TO $POSTGRES_USER;       -- 授予当前用户查询权限
    ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';  -- 设置预加载库
    ALTER SYSTEM SET pg_stat_statements.track = 'all';          -- 跟踪所有查询（含嵌套）
EOSQL
