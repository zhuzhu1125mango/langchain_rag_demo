"""
启动脚本 - RAG知识库问答系统启动器

本脚本负责：
1. 检查Ollama服务是否运行
2. 验证所需模型是否已安装
3. 检查数据库连接（PostgreSQL、MinIO、Milvus）
4. 配置日志系统
5. 启动FastAPI服务
"""

import os
import sys
import io

# 修复Windows控制台编码问题（在导入其他模块之前）
if sys.platform == 'win32':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except:
        pass

# 设置PostgreSQL客户端编码环境变量，解决Windows系统下的Unicode解码问题
os.environ['PGCLIENTENCODING'] = 'UTF8'
os.environ['LC_ALL'] = 'en_US.UTF-8'
os.environ['LANG'] = 'en_US.UTF-8'

import subprocess
import time
import logging
from logging.handlers import RotatingFileHandler

# 添加项目路径到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import settings, LOG_DIR

OLLAMA_MODEL_NAME = settings.model.OLLAMA_MODEL_NAME
EMBEDDING_MODEL_NAME = settings.model.EMBEDDING_MODEL_NAME
POSTGRES_USER = settings.database.POSTGRES_USER
POSTGRES_PASSWORD = settings.database.POSTGRES_PASSWORD
POSTGRES_HOST = settings.database.POSTGRES_HOST
POSTGRES_PORT = settings.database.POSTGRES_PORT
POSTGRES_DB = settings.database.POSTGRES_DB
MINIO_ENDPOINT = settings.minio.MINIO_ENDPOINT
MINIO_ACCESS_KEY = settings.minio.MINIO_ACCESS_KEY
MINIO_SECRET_KEY = settings.minio.MINIO_SECRET_KEY
MINIO_SECURE = settings.minio.MINIO_SECURE
MILVUS_HOST = settings.milvus.MILVUS_HOST
MILVUS_PORT = settings.milvus.MILVUS_PORT

# 获取环境变量配置
RELOAD_MODE = os.getenv("RELOAD_MODE", "false").lower() == "true"
# 支持命令行 --reload 参数开启开发热重载
if "--reload" in sys.argv:
    RELOAD_MODE = True
    sys.argv.remove("--reload")
SKIP_OLLAMA_CHECK = os.getenv("SKIP_OLLAMA_CHECK", "false").lower() == "true"
SKIP_ENV_CHECK = os.getenv("SKIP_ENV_CHECK", "false").lower() == "true"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "localhost")


def check_poetry_env():
    """
    检查是否在 Poetry 虚拟环境中运行
    
    Returns:
        bool: True表示在正确环境中
    """
    if SKIP_ENV_CHECK:
        return True
    
    import sys
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        if "pypoetry" in sys.prefix or "virtualenvs" in sys.prefix:
            return True
        else:
            print("[WARNING] 检测到虚拟环境，但不确定是否为 Poetry 环境")
            return True
    else:
        print("[ERROR] 请在 Poetry 虚拟环境中运行本脚本")
        print("")
        print("正确的启动方式：")
        print("  方式一：先激活虚拟环境")
        print("    poetry shell")
        print("    python start.py")
        print("")
        print("  方式二：直接使用 poetry run")
        print("    poetry run python start.py")
        print("")
        print("  方式三：使用 Poetry 脚本（推荐）")
        print("    poetry run start")
        print("")
        return False


def setup_logging():
    """
    配置日志系统
    
    设置控制台和文件日志，支持日志轮转
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # 日志格式
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # 创建日志记录器
    logger = logging.getLogger("rag_system")
    logger.setLevel(logging.INFO)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    
    # 文件处理器（轮转）
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "rag_system.log"),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(log_format))
    
    # 添加处理器
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger


def check_ollama_installed():
    """
    检查Ollama是否已安装
    
    Returns:
        bool: True表示Ollama已安装
    """
    try:
        result = subprocess.run(
            ["ollama", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError):
        return False


def check_ollama_running():
    """
    检查Ollama服务是否正在运行
    
    Returns:
        bool: True表示Ollama服务正在运行
    """
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError):
        return False


def check_ollama_remote(host="localhost", port=11434):
    """
    检查远程Ollama服务是否可用
    
    Args:
        host: Ollama服务地址
        port: Ollama服务端口
        
    Returns:
        bool: True表示Ollama服务可用
    """
    import httpx
    try:
        url = f"http://{host}:{port}/api/tags"
        response = httpx.get(url, timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def get_installed_models():
    """
    获取已安装的Ollama模型列表
    
    Returns:
        list: 模型名称列表
    """
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")[1:]  # 跳过表头
            models = [line.split()[0] for line in lines if line.strip()]
            return models
    except Exception:
        pass
    return []


def get_remote_models(host="localhost", port=11434):
    """
    获取远程Ollama服务的模型列表
    
    Args:
        host: Ollama服务地址
        port: Ollama服务端口
        
    Returns:
        list: 模型名称列表
    """
    import httpx
    try:
        url = f"http://{host}:{port}/api/tags"
        response = httpx.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return [model["name"] for model in data.get("models", [])]
    except Exception:
        pass
    return []


def pull_model(model_name):
    """
    拉取指定的Ollama模型
    
    Args:
        model_name: 模型名称
        
    Returns:
        bool: True表示拉取成功
    """
    try:
        print(f"正在拉取模型: {model_name}")
        result = subprocess.run(
            ["ollama", "pull", model_name],
            capture_output=True,
            text=True,
            timeout=300  # 5分钟超时
        )
        return result.returncode == 0
    except Exception as e:
        print(f"拉取模型失败: {e}")
        return False


def check_and_pull_models(logger):
    """
    检查并拉取所需模型
    
    Args:
        logger: 日志记录器
        
    Returns:
        bool: True表示所有模型都已就绪
    """
    required_models = [EMBEDDING_MODEL_NAME, OLLAMA_MODEL_NAME]
    
    if OLLAMA_HOST != "localhost":
        installed_models = get_remote_models(OLLAMA_HOST)
        logger.info(f"检查远程Ollama服务: {OLLAMA_HOST}")
    else:
        installed_models = get_installed_models()
    
    all_ready = True
    
    for model in required_models:
        if model not in installed_models:
            logger.warning(f"模型 {model} 未安装")
            if OLLAMA_HOST == "localhost":
                logger.warning(f"正在尝试拉取模型: {model}")
                if pull_model(model):
                    logger.info(f"模型 {model} 拉取成功")
                else:
                    logger.error(f"模型 {model} 拉取失败，请手动安装: ollama pull {model}")
                    all_ready = False
            else:
                logger.warning(f"远程Ollama服务，请在服务端手动安装模型: ollama pull {model}")
                all_ready = False
        else:
            logger.info(f"模型 {model} 已安装")
    
    return all_ready


def _safe_error_msg(e):
    """安全处理异常消息，避免编码问题"""
    try:
        error_type = type(e).__name__
        error_msg = str(e)
        try:
            clean_msg = error_msg.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
        except:
            clean_msg = repr(str(e))
        return f"{error_type}: {clean_msg}"
    except:
        return type(e).__name__


def check_postgresql(logger):
    """
    检查PostgreSQL数据库连接（修复Windows编码问题）
    
    Args:
        logger: 日志记录器
        
    Returns:
        bool: True表示连接成功
    """
    # 第一步：先检查端口是否开放
    import socket
    try:
        sock = socket.create_connection((POSTGRES_HOST, POSTGRES_PORT), timeout=5)
        sock.close()
        logger.info(f"PostgreSQL端口 {POSTGRES_HOST}:{POSTGRES_PORT} 可访问")
    except Exception as e:
        logger.error(f"PostgreSQL端口不可访问: {type(e).__name__}")
        return False
    
    # 第二步：尝试实际连接
    try:
        import psycopg2
        conn = psycopg2.connect(
            dbname=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            connect_timeout=30,
            options="-c client_encoding=UTF8"
        )
        conn.close()
        return True
    except ImportError:
        logger.error("psycopg2 未安装，请安装依赖: pip install psycopg2-binary")
        return False
    except UnicodeDecodeError:
        # Windows系统下的编码问题，psycopg2错误消息编码不兼容
        logger.error("PostgreSQL 连接失败（编码错误），请检查数据库配置")
        return False
    except Exception as e:
        # 安全处理异常消息，避免编码问题
        try:
            error_type = type(e).__name__
            error_msg = str(e)
            clean_msg = error_msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            logger.error(f"PostgreSQL 连接失败 [{error_type}]: {clean_msg}")
        except:
            logger.error("PostgreSQL 连接失败，请检查数据库配置")
        return False


def check_minio(logger):
    """
    检查MinIO服务连接
    
    Args:
        logger: 日志记录器
        
    Returns:
        bool: True表示连接成功
    """
    try:
        from minio import Minio
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE
        )
        client.list_buckets()
        return True
    except ImportError:
        logger.error("minio SDK 未安装，请安装依赖: pip install minio")
        return False
    except Exception as e:
        logger.error(f"MinIO 连接失败: {_safe_error_msg(e)}")
        return False


def check_milvus(logger):
    """
    检查Milvus服务连接（带重试机制）
    
    Args:
        logger: 日志记录器
        
    Returns:
        bool: True表示连接成功
    """
    max_retries = 5
    retry_delay = 10
    
    for attempt in range(max_retries):
        try:
            from pymilvus import MilvusClient
            client = MilvusClient(uri=f"http://{MILVUS_HOST}:{MILVUS_PORT}", timeout=30)
            client.list_collections()
            return True
        except ImportError:
            logger.error("pymilvus 未安装，请安装依赖: pip install pymilvus")
            return False
        except Exception as e:
            error_msg = _safe_error_msg(e)
            if "not ready" in error_msg.lower() or "service unavailable" in error_msg.lower():
                if attempt < max_retries - 1:
                    logger.warning(f"Milvus 服务未就绪 (尝试 {attempt + 1}/{max_retries})，等待 {retry_delay} 秒...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Milvus 连接失败: {error_msg}")
                    return False
            else:
                logger.error(f"Milvus 连接失败: {error_msg}")
                return False
    return False


def main():
    """
    启动主函数
    """
    if not check_poetry_env():
        sys.exit(1)
    
    # 配置日志
    logger = setup_logging()
    
    logger.info("=" * 60)
    logger.info("RAG Knowledge Base QA System - 启动检查")
    logger.info("=" * 60)
    
    # Ollama检查（可跳过）
    if not SKIP_OLLAMA_CHECK:
        # 检查Ollama安装（仅本地模式）
        if OLLAMA_HOST == "localhost":
            if not check_ollama_installed():
                logger.error("Ollama 未安装，请先安装 Ollama: https://ollama.com/download")
                sys.exit(1)
            logger.info("[OK] Ollama 已安装")
        
        # 检查Ollama服务
        if OLLAMA_HOST == "localhost":
            if not check_ollama_running():
                logger.warning("Ollama 服务未运行，正在启动...")
                try:
                    subprocess.Popen(["ollama", "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    time.sleep(3)  # 等待服务启动
                    if check_ollama_running():
                        logger.info("[OK] Ollama 服务已启动")
                    else:
                        logger.error("Ollama 服务启动失败，请手动启动: ollama serve")
                        sys.exit(1)
                except Exception as e:
                    logger.error(f"启动 Ollama 服务失败: {_safe_error_msg(e)}")
                    sys.exit(1)
            else:
                logger.info("[OK] Ollama 服务正在运行")
        else:
            if not check_ollama_remote(OLLAMA_HOST):
                logger.error(f"远程Ollama服务不可用: {OLLAMA_HOST}:11434")
                sys.exit(1)
            logger.info(f"[OK] 远程Ollama服务可用: {OLLAMA_HOST}")
        
        # 检查模型
        if not check_and_pull_models(logger):
            logger.warning("部分模型未就绪，服务将继续启动但可能影响功能")
    else:
        logger.info("[SKIP] Ollama 检查已跳过")
    
    # 检查PostgreSQL数据库
    if not check_postgresql(logger):
        logger.error("PostgreSQL 连接失败，请检查数据库配置")
        sys.exit(1)
    logger.info("[OK] PostgreSQL 连接成功")
    
    # 检查MinIO服务
    if not check_minio(logger):
        logger.error("MinIO 连接失败，请检查MinIO服务状态")
        sys.exit(1)
    logger.info("[OK] MinIO 连接成功")
    
    # 检查Milvus服务
    if not check_milvus(logger):
        logger.error("Milvus 连接失败，请检查Milvus服务状态")
        sys.exit(1)
    logger.info("[OK] Milvus 连接成功")
    
    logger.info("=" * 60)
    logger.info("所有检查通过，启动 FastAPI 服务...")
    logger.info("=" * 60)
    
    # 启动FastAPI服务
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=RELOAD_MODE,
        log_level="info"
    )


if __name__ == "__main__":
    main()
