"""运行时配置持久化（W4-23）。

/api/config/processing 修改的内存配置此前重启即失（内存态与 .env 静态基线
漂移，管理员修改被静默回滚）。本模块把运行时覆盖快照写入
``backend/data/runtime_config.json``（容器内 /app/data，随 compose 挂载卷
持久化，路径语义同 LOG_DIR），启动时重新应用。

仅支持白名单键（processing 调优参数）；.env 仍是静态基线——文件只存
"在基线上被 UI 改过的值"。文件损坏时忽略覆盖按基线启动，不阻断服务。
"""

import json
import logging
import os
import tempfile
from typing import Any, Dict

from src.config import BASE_DIR

logger = logging.getLogger(__name__)

# 运行时可覆盖的配置白名单：settings 节名 -> {键: 期望类型}
_OVERRIDABLE: Dict[str, Dict[str, type]] = {
    "processing": {
        "CHUNK_SIZE": int,
        "CHUNK_OVERLAP": int,
        "TOP_K": int,
    },
}

# 后端根 data 目录（本地 backend/data；容器 /app/data，为挂载持久卷）
_DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "data"))
_OVERRIDES_PATH = os.path.join(_DATA_DIR, "runtime_config.json")


def load_runtime_overrides() -> None:
    """启动时加载运行时覆盖；文件不存在/损坏时静默跳过。"""
    if not os.path.exists(_OVERRIDES_PATH):
        return
    try:
        with open(_OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("顶层必须是对象")
        from src.config import settings

        applied = []
        for section, values in data.items():
            if section in _OVERRIDABLE and isinstance(values, dict):
                target = getattr(settings, section)
                changed = False
                for key, value in values.items():
                    expected = _OVERRIDABLE[section].get(key)
                    if expected is None:
                        continue
                    try:
                        setattr(target, key, expected(value))
                        changed = True
                    except (TypeError, ValueError):
                        logger.warning("忽略非法运行时覆盖值 [%s.%s=%r]", section, key, value)
                if changed:
                    applied.append(section)
        if applied:
            logger.info(
                "已加载运行时配置覆盖: %s（落盘于 %s；.env 为静态基线）",
                ", ".join(applied), _OVERRIDES_PATH,
            )
    except (OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("运行时配置文件读取失败，忽略覆盖（按 .env 基线启动）: %s", e)


def save_runtime_overrides() -> None:
    """把当前内存中的可覆盖配置快照原子落盘（写临时文件后 replace）。

    落盘失败仅告警不抛出：内存值已生效，仅重启后会回退基线。
    """
    from src.config import settings

    snapshot: Dict[str, Dict[str, Any]] = {}
    for section, keys in _OVERRIDABLE.items():
        target = getattr(settings, section)
        snapshot[section] = {key: getattr(target, key) for key in keys}

    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=_DATA_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, _OVERRIDES_PATH)
        finally:
            # replace 成功后 tmp 已不存在，exists 判断保证失败路径才清理
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        logger.info("运行时配置已落盘: %s", _OVERRIDES_PATH)
    except OSError as e:
        logger.warning("运行时配置落盘失败（内存值仍生效，重启后回退 .env 基线）: %s", e)
