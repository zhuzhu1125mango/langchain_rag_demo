"""搜索 Agent 基类：JSON 抽取、工具执行与来源归并（CODE-1 拆分）。"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List


logger = logging.getLogger(__name__)

from src.utils.error_handling import log_exception

from src.services.search_agent_types import ToolCall, ToolResult, _extract_domain
from src.services.search_toolkit import SearchToolkit

class BaseSearchAgent:
    """Agent 基类，提供通用解析与工具执行能力"""

    def __init__(self, llm, toolkit: SearchToolkit):
        self.llm = llm
        self.toolkit = toolkit

    def _extract_json_blocks(self, content: str) -> List[Dict[str, Any]]:
        """从文本中提取 JSON 对象/数组块"""
        results = []

        # 先尝试提取 <tool_call>...</tool_call> 标签
        tag_pattern = r"<tool_call>\s*([\s\S]*?)\s*</tool_call>"
        for match in re.findall(tag_pattern, content):
            match = match.strip()
            if not match:
                continue
            try:
                data = json.loads(match)
                results.extend(self._normalize_json_to_dicts(data))
            except Exception as e:
                logger.debug(f"标签内 JSON 解析失败: {e}")

        if results:
            return results

        # 再尝试整段解析（处理模型直接返回纯 JSON 的情况）
        stripped = content.strip()
        if stripped.startswith(("{", "[")):
            try:
                data = json.loads(stripped)
                return self._normalize_json_to_dicts(data)
            except Exception:
                pass

        # 最后尝试提取独立的 JSON 对象/数组（非贪婪，适用于简单情况）
        json_pattern = r"(\{[\s\S]*?\}|\[[\s\S]*?\])"
        for match in re.findall(json_pattern, content):
            try:
                data = json.loads(match)
                results.extend(self._normalize_json_to_dicts(data))
            except Exception:
                continue

        return results

    def _normalize_json_to_dicts(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    async def _execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        tool = self.toolkit.get_tool(tool_call.name)
        if not tool:
            return ToolResult(
                tool_call=tool_call,
                result=f"工具 '{tool_call.name}' 不存在",
                success=False,
            )

        try:
            result = await tool.handler(**tool_call.arguments)
            return ToolResult(tool_call=tool_call, result=result, success=True)
        except Exception as exc:
            # 工具失败可能是外部抖动（可恢复）也可能是 handler 自身缺陷（编程错误），
            # 交给分级判定决定日志级别与是否值得重试
            log_exception(
                logger,
                exc,
                action="agent.tool.execute",
                message="工具执行失败",
                tool=tool_call.name,
            )
            return ToolResult(tool_call=tool_call, result=f"执行失败: {exc}", success=False)

    async def _execute_tool_calls(self, tool_calls: List[ToolCall]) -> List[ToolResult]:
        if not tool_calls:
            return []

        tasks = [self._execute_tool_call(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    def _extract_sources_from_texts(self, texts: List[str]) -> List[Dict[str, Any]]:
        """从文本列表中提取 URL 来源信息（_extract_sources / _extract_sources_from_steps 的公共逻辑）

        web_search 工具返回格式为 "[{i}] 标题: {title}\\nURL: {url}\\n摘要: {content}"，
        多条结果以空行分隔。本方法按段落切分文本，逐段提取 URL 与标题，确保
        多结果场景下 title 与 url 正确对应；title 缺失时回退为域名，再回退为"网页来源"。
        """
        sources: List[Dict[str, Any]] = []
        url_pattern = re.compile(r"URL:\s*(https?://\S+)", re.IGNORECASE)
        title_pattern = re.compile(r"标题:\s*(.+)", re.IGNORECASE)

        for text in texts:
            # 按空行切分段落，每段对应一条搜索结果（标题+URL+摘要）
            segments = re.split(r"\n\s*\n", text)
            for seg in segments:
                url_match = url_pattern.search(seg)
                if not url_match:
                    continue
                url = url_match.group(1).strip()
                # 去重：同一 URL 只保留首次出现
                if any(s.get("url") == url for s in sources):
                    continue
                # 提取标题：优先匹配 "标题: xxx"，缺失时回退到域名，再回退到"网页来源"
                title = ""
                title_match = title_pattern.search(seg)
                if title_match:
                    title = title_match.group(1).strip()
                domain = _extract_domain(url)
                if not title:
                    title = domain or "网页来源"
                sources.append({
                    "url": url,
                    "source": domain,
                    "title": title,
                    "document_id": "",
                    "filename": "web_search",
                    "chunk_index": 0,
                    "total_chunks": 1,
                    "page_content": seg[:500],
                })

        return sources

    def _extract_sources(self, tool_results: List[ToolResult]) -> List[Dict[str, Any]]:
        """从工具结果中提取来源信息"""
        texts = [tr.result for tr in tool_results if tr.success]
        return self._extract_sources_from_texts(texts)
