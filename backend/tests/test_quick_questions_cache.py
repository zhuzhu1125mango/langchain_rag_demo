"""快捷问题缓存 LRU 容量上限与过期清理单元测试（#13 残留）"""

from datetime import datetime, timedelta

from src.api import session as session_module
from src.api.session import (
    _QUICK_QUESTIONS_CACHE_MAX_SIZE,
    _QUICK_QUESTIONS_CACHE_TTL,
    _get_cached_quick_questions,
    _quick_questions_cache,
    _set_cached_quick_questions,
)


def setup_function():
    """每条用例前清空缓存，避免相互污染。"""
    _quick_questions_cache.clear()


def test_max_size_constant():
    """容量上限常量已定义且为合理值。"""
    assert _QUICK_QUESTIONS_CACHE_MAX_SIZE == 256


def test_evicts_oldest_when_over_capacity():
    """写入超过上限条目时，最旧（最久未使用）条目被淘汰，容量不超限。"""
    for i in range(_QUICK_QUESTIONS_CACHE_MAX_SIZE + 10):
        _set_cached_quick_questions([f"问题 {i}"], [f"建议 {i}"])

    assert len(_quick_questions_cache) == _QUICK_QUESTIONS_CACHE_MAX_SIZE

    # 最早的条目 i=0..9 已被淘汰
    assert _get_cached_quick_questions(["问题 0"]) is None
    assert _get_cached_quick_questions(["问题 9"]) is None
    # 最新的条目仍在
    assert _get_cached_quick_questions(["问题 259"]) is not None
    assert _get_cached_quick_questions(["问题 265"]) is not None


def test_hit_refreshes_lru_order():
    """命中会刷新 LRU 顺序：较早写入但最近被命中的条目不先于新条目淘汰。"""
    # 写满容量，条目 0 最旧
    for i in range(_QUICK_QUESTIONS_CACHE_MAX_SIZE):
        _set_cached_quick_questions([f"问题 {i}"], [f"建议 {i}"])

    # 命中条目 0，使其移到末尾（最近使用）
    assert _get_cached_quick_questions(["问题 0"]) is not None

    # 再写入 1 条触发淘汰：应淘汰未命中过的条目 1，而非刚命中的条目 0
    _set_cached_quick_questions(["新问题"], ["新建议"])

    assert len(_quick_questions_cache) == _QUICK_QUESTIONS_CACHE_MAX_SIZE
    assert _get_cached_quick_questions(["问题 0"]) is not None
    assert _get_cached_quick_questions(["问题 1"]) is None


def test_write_lazily_removes_expired_entries():
    """写时惰性清理：过期条目在写入时被移除，即使未被访问。"""
    key = session_module._get_quick_questions_cache_key
    k = key(["过期问题"])
    _quick_questions_cache[k] = (datetime.now() - _QUICK_QUESTIONS_CACHE_TTL - timedelta(minutes=1), ["旧建议"])

    assert k in _quick_questions_cache

    # 写入新条目触发惰性清理
    _set_cached_quick_questions(["新问题"], ["新建议"])

    assert k not in _quick_questions_cache
    assert _get_cached_quick_questions(["新问题"]) == ["新建议"]


def test_expired_entry_returns_none():
    """TTL 过期后读取返回 None，且条目被移除。"""
    key = session_module._get_quick_questions_cache_key
    _set_cached_quick_questions(["过期问题"], ["建议"])

    k = key(["过期问题"])
    # 手动将缓存时间回拨为过期
    cached_at, suggestions = _quick_questions_cache[k]
    _quick_questions_cache[k] = (cached_at - _QUICK_QUESTIONS_CACHE_TTL - timedelta(seconds=1), suggestions)

    assert _get_cached_quick_questions(["过期问题"]) is None
    assert k not in _quick_questions_cache


def test_same_key_overwrite_keeps_single_entry():
    """相同历史问题重复写入只保留一条，并刷新缓存时间。"""
    for _ in range(3):
        _set_cached_quick_questions(["问题 A", "问题 B"], ["建议"])

    assert len(_quick_questions_cache) == 1
    assert _get_cached_quick_questions(["问题 A", "问题 B"]) == ["建议"]
