"""
监控中间件 - 响应时间和错误率监控

本模块负责：
1. 记录每个请求的响应时间
2. 统计API调用次数和错误率
3. 提供性能指标监控接口
4. 集成 Prometheus 指标记录
"""

import asyncio
import time
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from collections import defaultdict
import logging

logger = logging.getLogger("rag_system")

# 导入 Prometheus 指标模块
try:
    from .prometheus import record_request, ACTIVE_REQUESTS
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

# 监控指标存储
metrics = {
    "request_count": 0,
    "error_count": 0,
    "total_response_time": 0.0,
    "endpoint_stats": defaultdict(lambda: {
        "count": 0,
        "errors": 0,
        "total_time": 0.0,
        "min_time": float("inf"),
        "max_time": 0.0
    })
}


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    性能监控中间件
    
    记录每个请求的响应时间、错误率等指标，同时集成 Prometheus 指标记录
    """
    
    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()
        endpoint = f"{request.method} {request.url.path}"
        
        # 增加活跃请求数（Prometheus）
        if PROMETHEUS_AVAILABLE:
            ACTIVE_REQUESTS.inc()
        
        status_code = 500
        
        try:
            response = await call_next(request)
            status_code = response.status_code

            if status_code >= 400:
                metrics["error_count"] += 1
                metrics["endpoint_stats"][endpoint]["errors"] += 1

            return response

        except asyncio.CancelledError:
            # 服务器关闭/请求取消时不计入错误，直接向上传播
            raise

        except Exception as e:
            metrics["error_count"] += 1
            metrics["endpoint_stats"][endpoint]["errors"] += 1
            raise
        finally:
            response_time = time.time() - start_time
            metrics["request_count"] += 1
            metrics["total_response_time"] += response_time
            
            # 更新端点统计
            endpoint_stat = metrics["endpoint_stats"][endpoint]
            endpoint_stat["count"] += 1
            endpoint_stat["total_time"] += response_time
            endpoint_stat["min_time"] = min(endpoint_stat["min_time"], response_time)
            endpoint_stat["max_time"] = max(endpoint_stat["max_time"], response_time)
            
            # 记录慢请求日志（超过5秒）
            if response_time > 5.0:
                logger.warning(f"慢请求警告: {endpoint} - {response_time:.2f}s")
            
            # 记录 Prometheus 指标
            if PROMETHEUS_AVAILABLE:
                record_request(request.method, str(request.url.path), status_code, response_time)
                ACTIVE_REQUESTS.dec()


def get_metrics():
    """
    获取当前监控指标
    
    Returns:
        dict: 包含所有监控指标的字典
    """
    avg_response_time = (
        metrics["total_response_time"] / metrics["request_count"]
        if metrics["request_count"] > 0 else 0.0
    )
    
    error_rate = (
        metrics["error_count"] / metrics["request_count"] * 100
        if metrics["request_count"] > 0 else 0.0
    )
    
    # 计算每个端点的统计
    endpoint_metrics = {}
    for endpoint, stats in metrics["endpoint_stats"].items():
        if stats["count"] > 0:
            endpoint_metrics[endpoint] = {
                "count": stats["count"],
                "errors": stats["errors"],
                "error_rate": (stats["errors"] / stats["count"]) * 100,
                "avg_time": stats["total_time"] / stats["count"],
                "min_time": stats["min_time"],
                "max_time": stats["max_time"]
            }
    
    return {
        "request_count": metrics["request_count"],
        "error_count": metrics["error_count"],
        "error_rate": error_rate,
        "avg_response_time": avg_response_time,
        "total_response_time": metrics["total_response_time"],
        "endpoints": endpoint_metrics
    }


def reset_metrics():
    """
    重置所有监控指标
    """
    metrics["request_count"] = 0
    metrics["error_count"] = 0
    metrics["total_response_time"] = 0.0
    metrics["endpoint_stats"] = defaultdict(lambda: {
        "count": 0,
        "errors": 0,
        "total_time": 0.0,
        "min_time": float("inf"),
        "max_time": 0.0
    })