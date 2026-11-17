"""用户可选模型目录。

与 `admin_models` 的分工：那个是管理员配置面（provider 密钥、成本、路由），
全部 `require_admin`；这里是**用户面**，只回答一件事——「我能选哪些模型」。

为什么单独一个端点而不是复用 `/admin/models/deployments`：
管理面会返回 provider_id、单价、base_url 这些不该给普通用户看的东西。
过滤做在端点层比做在前端可靠——前端过滤只是隐藏，不是隔离。

只暴露 `user_selectable=true` 且具备 `chat` 能力的模型：
- `user_selectable` 默认 false（见 a7b8c9d0e1f2 迁移），要开放哪些由管理员决定
- embedding / rerank 模型即使被标可选也不该出现在聊天选择器里
"""

import logging

from fastapi import APIRouter, Depends

from src.auth import CurrentUser, get_current_user
from src.services.model_manager import model_manager
from src.services.model_registry import resolve_thinking_control

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/available")
async def list_available_models(current_user: CurrentUser = Depends(get_current_user)):
    """当前用户可选的对话模型。

    返回里的 `default_model_id` 是「不传 model_id 时实际用的模型」，
    UI 靠它把「默认」标出来——否则用户看不出自己正在用哪一个。
    """
    registry = model_manager.registry

    # 「默认」必须标在用户实际会用的那个模型上。`deep_thinking` 默认 off，
    # 生成走 `answer_direct` 路由；只有它缺失时才回退到 `answer`（思考路径）。
    # 标错不会报错，只会让人以为默认在用的是思考模型——实测两者延迟差 3 倍以上。
    default_model_id = ""
    default_thinking_control = ""
    for task in ("answer_direct", "answer"):
        try:
            primary = registry.resolve(task).primary
            default_model_id = primary.model.id
            default_thinking_control = resolve_thinking_control(primary.model, primary.provider)
            break
        except Exception:  # noqa: BLE001 —— 单条路由缺失不影响目录可用，继续试下一条
            logger.warning("模型目录：%s 路由解析失败", task)

    def _thinking_control(model_id: str) -> str:
        resolved = registry.get_model(model_id)
        return resolve_thinking_control(resolved.model, resolved.provider)

    models = [
        {
            "id": model.id,
            "display_name": model.display_name or model.model,
            "capabilities": list(model.capabilities),
            "context_window": model.context_window,
            "is_default": model.id == default_model_id,
            # off：不会思考；switchable：能开能关；always_on：会思考但关不掉。
            # 客户端据此决定深度思考开关可不可点——「关不掉」和「不会思考」
            # 都要置灰，但提示语不同，所以给枚举而不是布尔。
            "thinking_control": _thinking_control(model.id),
        }
        for model in registry.config.models
        if model.user_selectable and model.has("chat")
    ]
    return {
        "models": models,
        "default_model_id": default_model_id,
        # 默认模型可能没开放给用户（不在 models 里），前端就拿不到它的可控性；
        # 而「不选模型」时用的正是它。空串 = 没有默认路由，前端按未知处理。
        "default_thinking_control": default_thinking_control,
        "source": registry.source,
    }
