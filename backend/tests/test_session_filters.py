"""会话列表筛选条件的单测（不连库）。

能测什么、不能测什么，先说清楚：
- **能**测「生成的 SQL 长什么样」和「关键词里的通配符有没有被转义」——
  这两件事是纯逻辑，错了会静默给出错误结果（搜 `100%` 匹配到全部），必须钉住；
- **不能**测真实命中行数，那需要 PostgreSQL，由 `test_api_session.py` 的集成测试覆盖。
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.api.session import (
    SESSION_SEARCH_MAX_KEYWORD,
    _build_session_filters,
    _escape_like,
)
from src.models import Session as SessionModel


def _compile(**kwargs):
    """编译成 PostgreSQL SQL 与绑定参数。

    参数不内联成字面量：`literal_binds` 对 DateTime 会报 NotSupportedError，
    而且断言 SQL 结构（ILIKE / @> / 区间运算符）+ 断言参数值本来就是更稳的写法。
    """
    defaults = {"user_id": "u1", "keyword": None, "kb_id": None, "date_from": None, "date_to": None}
    defaults.update(kwargs)
    conditions = _build_session_filters(**defaults)
    stmt = select(SessionModel.id).where(*conditions)
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def _sql(**kwargs) -> str:
    return _compile(**kwargs)[0]


def test_no_filters_only_scopes_to_user():
    """不传筛选参数时必须退化为「只看自己的」，这是安全边界不是可选项。"""
    sql = _sql()
    assert "sessions.user_id = " in sql
    assert "ILIKE" not in sql
    assert "@>" not in sql


def test_keyword_searches_title_and_message_body():
    sql = _sql(keyword="打卡")
    assert "sessions.title ILIKE" in sql
    assert "CAST(sessions.messages AS TEXT) ILIKE" in sql
    # 两个条件是 OR 关系：只搜标题会漏掉「标题是自动生成的，关键词只在正文里」的情况
    assert " OR " in sql


def test_keyword_wildcards_are_escaped():
    """用户搜 `100%` 不该变成通配符——否则会匹配到所有以 100 开头的内容。"""
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a_b") == "a\\_b"
    # 反斜杠必须先转义，否则会把自己新加的转义符再转义一遍
    assert _escape_like("a\\b") == "a\\\\b"


def test_keyword_is_trimmed_and_length_capped():
    """超长关键词会被截断：LIKE 模式越长越慢，且用户不会输 200 字搜历史。"""
    _, params = _compile(keyword="  " + "长" * (SESSION_SEARCH_MAX_KEYWORD + 50) + "  ")
    patterns = [v for v in params.values() if isinstance(v, str) and "长" in v]
    assert patterns, params
    assert all(v == f"%{'长' * SESSION_SEARCH_MAX_KEYWORD}%" for v in patterns)


def test_blank_keyword_is_ignored():
    """`q=` 或 `q=   ` 应当等同于没传，而不是匹配空串。"""
    assert "ILIKE" not in _sql(keyword="   ")


def test_kb_filter_casts_json_column_to_jsonb():
    """kb_ids 列是 JSON 不是 JSONB，不转类型就用不了 @> 包含运算符。"""
    sql = _sql(kb_id="kb-1")
    assert "CAST(sessions.kb_ids AS JSONB) @>" in sql


def test_date_range_uses_half_open_interval():
    """上界用「次日零点」开区间，避免 23:59:59 这类边界把当天数据漏掉。"""
    sql, params = _compile(date_from=date(2026, 11, 1), date_to=date(2026, 11, 5))
    assert "sessions.updated_at >=" in sql
    assert "sessions.updated_at <" in sql
    bounds = sorted(v for v in params.values() if isinstance(v, datetime))
    assert bounds == [
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 6, tzinfo=timezone.utc),  # date_to + 1 天
    ]


def test_all_filters_combine_with_and():
    sql = _sql(keyword="打卡", kb_id="kb-1", date_from=date(2026, 11, 1))
    assert "sessions.user_id = " in sql
    assert "CAST(sessions.messages AS TEXT) ILIKE" in sql
    assert "@>" in sql
    assert "sessions.updated_at >=" in sql
