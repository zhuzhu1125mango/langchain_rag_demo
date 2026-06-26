"""
FeedbackRequest 评分字段校验测试
"""

from src.api.chat import FeedbackRequest


def test_int_rating_accepted():
    """整数评分 1-5 应被接受，且类型保持为 int"""
    for rating in [1, 2, 3, 4, 5]:
        req = FeedbackRequest(rating=rating, reason='', session_id=None)
        assert req.rating == rating
        assert isinstance(req.rating, int)


def test_string_sentiment_rating_accepted():
    """字符串情感评分应被接受，且类型保持为 str"""
    for rating in ['like', 'dislike', 'positive', 'negative']:
        req = FeedbackRequest(rating=rating, reason='', session_id=None)
        assert req.rating == rating
        assert isinstance(req.rating, str)


def test_string_number_rating_coerced_to_int():
    """字符串数字评分应被接受，并被强制转换为 int"""
    for rating in ['1', '2', '3', '4', '5']:
        req = FeedbackRequest(rating=rating, reason='', session_id=None)
        assert req.rating == int(rating)
        assert isinstance(req.rating, int)


def test_invalid_string_rating_accepted_as_str():
    """无效字符串评分在 schema 层不触发校验，被当作 str 接受

    注：真正的取值校验在 endpoint 处理逻辑中完成，而非 pydantic 模型层。
    """
    req = FeedbackRequest(rating='invalid', reason='', session_id=None)
    assert req.rating == 'invalid'
    assert isinstance(req.rating, str)
