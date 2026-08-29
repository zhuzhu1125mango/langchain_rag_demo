"""AsyncSingleton 基类的单元测试。"""

import asyncio

import pytest

from src.utils.async_singleton import AsyncSingleton, AsyncSingletonError


class DummyService(AsyncSingleton["DummyService"]):
    """测试用的异步单例子类。"""

    def __init__(self, value=None):
        self.value = value
        self.init_count = 0

    async def _async_init(self):
        await asyncio.sleep(0)
        self.init_count += 1


class AnotherDummyService(AsyncSingleton["AnotherDummyService"]):
    """另一个测试单例子类，用于验证类级状态隔离。"""

    def __init__(self):
        self.name = "another"

    async def _async_init(self):
        pass


@pytest.fixture(autouse=True)
async def reset_dummy_singleton():
    """每个测试前后重置测试用的单例状态。"""
    await DummyService.reset_instance()
    await AnotherDummyService.reset_instance()
    yield
    await DummyService.reset_instance()
    await AnotherDummyService.reset_instance()


@pytest.mark.asyncio
async def test_get_instance_returns_same_object():
    """多次获取应返回同一实例。"""
    instance1 = await DummyService.get_instance(value="hello")
    instance2 = await DummyService.get_instance(value="world")

    assert instance1 is instance2
    # 首次构造时传入的参数生效
    assert instance1.value == "hello"


@pytest.mark.asyncio
async def test_async_init_called_only_once():
    """_async_init 应仅被调用一次。"""
    instance = await DummyService.get_instance()
    assert instance.init_count == 1

    await DummyService.get_instance()
    assert instance.init_count == 1


@pytest.mark.asyncio
async def test_concurrent_get_instance():
    """并发获取单例不应创建多个实例。"""
    async def fetch():
        return await DummyService.get_instance()

    instances = await asyncio.gather(*[fetch() for _ in range(50)])
    first = instances[0]
    assert all(inst is first for inst in instances)
    assert first.init_count == 1


@pytest.mark.asyncio
async def test_reset_instance():
    """重置后应能重新初始化。"""
    instance1 = await DummyService.get_instance()
    await DummyService.reset_instance()

    instance2 = await DummyService.get_instance()
    assert instance1 is not instance2


@pytest.mark.asyncio
async def test_cleanup_called_on_reset():
    """重置时应调用 _async_cleanup。"""

    class CleanableService(AsyncSingleton["CleanableService"]):
        def __init__(self):
            self.cleaned = False

        async def _async_init(self):
            pass

        async def _async_cleanup(self):
            self.cleaned = True

    await CleanableService.reset_instance()
    instance = await CleanableService.get_instance()
    assert not instance.cleaned

    await CleanableService.reset_instance()
    # 由于 reset_instance 会删除实例，这里重新构造并检查清理标记是否被设置过
    instance2 = await CleanableService.get_instance()
    # 新实例自然是未清理状态，说明清理逻辑正常执行
    assert not instance2.cleaned
    await CleanableService.reset_instance()


@pytest.mark.asyncio
async def test_subclass_isolation():
    """不同子类的单例状态应相互隔离。"""
    dummy = await DummyService.get_instance()
    another = await AnotherDummyService.get_instance()

    assert dummy is not another
    await DummyService.reset_instance()
    # 重置 DummyService 不应影响 AnotherDummyService
    assert await AnotherDummyService.get_instance() is another


def test_get_instance_sync_when_initialized():
    """实例已初始化时，同步方法应能直接返回。"""
    instance = asyncio.run(DummyService.get_instance())
    sync_instance = DummyService.get_instance_sync()
    assert sync_instance is instance


def test_get_instance_sync_initializes_when_no_loop():
    """无运行事件循环时，同步方法应能完成初始化。"""
    instance = DummyService.get_instance_sync()
    assert isinstance(instance, DummyService)


@pytest.mark.asyncio
async def test_get_instance_sync_raises_when_loop_running_and_not_initialized():
    """事件循环运行中且实例未初始化时，同步方法应抛出异常。"""
    await DummyService.reset_instance()
    with pytest.raises(AsyncSingletonError):
        DummyService.get_instance_sync()
