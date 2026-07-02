"""搜索相关的基础数据类型。

将 SearchResult 等跨模块使用的类型集中定义，避免 web_search_service
与 search_postprocessor 之间的循环导入。
"""

from dataclasses import dataclass


@dataclass
class SearchResult:
    """单条搜索结果。"""

    title: str
    url: str
    content: str
    source: str = "web_search"
    engine: str = ""
