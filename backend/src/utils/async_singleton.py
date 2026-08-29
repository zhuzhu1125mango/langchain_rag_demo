"""通用异步单例基类与装饰器。

为需要跨请求保持唯一实例的服务类提供统一的异步单例实现，
支持延迟初始化、并发安全、以及测试友好的重置能力。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


class AsyncSingletonError(Exception):
    """异步单例相关的异常。"""

    pass


class AsyncSingleton(Generic[T]):
    """通用异步单例基类。

    子类只需继承本类，并实现 ``_async_init`` 方法即可拥有统一、并发安全的
    单例能力。典型用法：

        class CacheService(AsyncSingleton["CacheService"]):
            async def _async_init(self):
                self.client = await self._build_client()

        service = await CacheService.get_instance()

    如果类在 ``__init__`` 中需要接收参数，可通过 ``get_instance(*args, **kwargs)``
    传入；这些参数仅在首次创建实例时使用。
    """

    # 类级状态，按子类维度隔离
    _instances: dict[type, Any] = {}
    _locks: dict[type, asyncio.Lock] = {}
    _initialized: dict[type, bool] = {}

    def __init_subclass__(cls, **kwargs: Any):
        super().__init_subclass__(**kwargs)
        cls._locks[cls] = asyncio.Lock()
        cls._initialized[cls] = False

    @classmethod
    async def get_instance(cls, *args: Any, **kwargs: Any) -> T:
        """获取类的单例实例，首次调用时执行异步初始化。

        Args:
            *args: 首次构造实例时传递给 ``__init__`` 的位置参数。
            **kwargs: 首次构造实例时传递给 ``__init__`` 的关键字参数。

        Returns:
            唯一实例。
        """
        if cls._initialized.get(cls, False):
            return cls._instances[cls]

        lock = cls._locks.setdefault(cls, asyncio.Lock())
        async with lock:
            # 双重检查锁定，避免多个协程重复初始化
            if cls._initialized.get(cls, False):
                return cls._instances[cls]

            if cls not in cls._instances:
                instance = cls(*args, **kwargs)
                cls._instances[cls] = instance

            instance = cls._instances[cls]
            await instance._async_init()
            cls._initialized[cls] = True
            logger.debug(f"{cls.__name__} 单例初始化完成")
            return instance

    @classmethod
    async def reset_instance(cls) -> None:
        """重置单例实例。

        主要用于测试场景或需要强制重新初始化配置的场景。会调用实例的
        ``_async_cleanup`` 方法（如果存在）以释放资源。
        """
        lock = cls._locks.setdefault(cls, asyncio.Lock())
        async with lock:
            if cls not in cls._instances:
                cls._initialized[cls] = False
                return

            instance = cls._instances[cls]
            try:
                if hasattr(instance, "_async_cleanup"):
                    cleanup = getattr(instance, "_async_cleanup")
                    if asyncio.iscoroutinefunction(cleanup):
                        await cleanup()
            except Exception as e:
                logger.warning(f"{cls.__name__} 单例清理失败: {e}", exc_info=True)
            finally:
                del cls._instances[cls]
                cls._initialized[cls] = False
                logger.debug(f"{cls.__name__} 单例已重置")

    @classmethod
    def get_instance_sync(cls, *args: Any, **kwargs: Any) -> T:
        """同步方式获取单例实例。

        用于必须在同步代码中获取单例的场景（如同步工具函数）。如果实例已
        初始化则直接返回；如果实例未初始化，会尝试使用当前事件循环或创建
        新的事件循环完成异步初始化。

        注意：此方法不应在已经运行的事件循环中调用，否则可能抛出异常。
        在异步代码中请优先使用 ``get_instance``。

        Args:
            *args: 首次构造实例时传递给 ``__init__`` 的位置参数。
            **kwargs: 首次构造实例时传递给 ``__init__`` 的关键字参数。

        Returns:
            唯一实例。
        """
        if cls._initialized.get(cls, False):
            return cls._instances[cls]

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            raise AsyncSingletonError(
                f"{cls.__name__} 未初始化，无法在运行中的事件循环中通过同步方式初始化。"
                "请在异步上下文中使用 await cls.get_instance()。"
            )

        return asyncio.run(cls.get_instance(*args, **kwargs))

    async def _async_init(self) -> None:
        """异步初始化方法，子类应覆盖此方法实现具体初始化逻辑。

        该方法在 ``get_instance`` 首次完成实例构造后调用，且仅调用一次。
        """
        pass

    async def _async_cleanup(self) -> None:
        """异步清理方法，子类可覆盖此方法释放资源。

        该方法仅在 ``reset_instance`` 时被调用。
        """
        pass


def async_singleton(init_func: Optional[Callable[..., Awaitable[Any]]] = None) -> Callable:
    """异步单例装饰器（类装饰器）。

    适用于希望保持类定义简洁，或不想显式继承 ``AsyncSingleton`` 的场景。
    被装饰的类必须提供一个异步的 ``_async_init`` 方法。

    用法示例：

        @async_singleton
        class MyService:
            async def _async_init(self):
                self.resource = await create_resource()

        instance = await MyService.get_instance()

    Args:
        init_func: 用于初始化的异步函数，接收类实例作为第一个参数。

    Returns:
        包装后的类。
    """

    def decorator(cls: type) -> type:
        class SingletonWrapper(cls, AsyncSingleton):
            pass

        SingletonWrapper.__name__ = cls.__name__
        SingletonWrapper.__qualname__ = cls.__qualname__
        SingletonWrapper.__module__ = cls.__module__
        SingletonWrapper.__doc__ = cls.__doc__

        if init_func is not None:

            async def _async_init(self) -> None:
                await init_func(self)

            SingletonWrapper._async_init = _async_init

        return SingletonWrapper

    return decorator
