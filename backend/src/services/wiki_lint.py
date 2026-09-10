"""Wiki 体检报告（P3 LLM-Wiki 编译层 Phase 3，§11.3）。

规则级零 LLM、同步毫秒级；只报告不自动修复，供用户决定是否重编译。

| 检查项   | 级别    | 规则                                          |
|----------|---------|---------------------------------------------|
| 断链     | error   | links 指向的 title 无对应 active 页            |
| 孤立页   | warning | 不在 index 页目录、且无任何其他页链接指向它      |
| 目录缺失 | warning | active 实体/主题页未收录进 index 页目录          |
| 空源页   | warning | active 实体/主题页但 source_doc_ids=[]          |
| 异常体量 | info    | 正文 < 200 字或 > 20000 字                      |
"""

import logging
from dataclasses import dataclass, field
from typing import List

from src.services.wiki_compiler import _WIKI_LINK_RE, normalize_title

logger = logging.getLogger("wiki_lint")

# 异常体量阈值（字符）
SIZE_TOO_SHORT = 200
SIZE_TOO_LONG = 20000

RULE_BROKEN_LINK = "broken_link"
RULE_ORPHAN_PAGE = "orphan_page"
RULE_MISSING_IN_INDEX = "missing_in_index"
RULE_EMPTY_SOURCE = "empty_source"
RULE_ABNORMAL_SIZE = "abnormal_size"


@dataclass
class LintIssue:
    """单条体检问题。"""

    rule: str
    level: str  # error / warning / info
    page_id: str
    title: str
    message: str

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "level": self.level,
            "page_id": self.page_id,
            "title": self.title,
            "message": self.message,
        }


@dataclass
class LintReport:
    """体检报告（含各规则问题明细）。"""

    kb_id: str
    checked_pages: int = 0
    issues: List[LintIssue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kb_id": self.kb_id,
            "checked_pages": self.checked_pages,
            "issues": [i.to_dict() for i in self.issues],
        }


async def lint_kb_wiki(db, kb_id: str) -> LintReport:
    """对指定 KB 的 active Wiki 页执行 5 项规则检查；无页面返回空报告。"""
    from sqlalchemy import select

    from src.models.wiki_page import WikiPage

    report = LintReport(kb_id=str(kb_id))

    rows = (
        await db.execute(
            select(WikiPage).filter(
                WikiPage.kb_id == str(kb_id),
                WikiPage.status == "active",
            )
        )
    ).scalars().all()
    entity_rows = [r for r in rows if r.page_type in ("entity", "topic")]
    report.checked_pages = len(rows)
    if not rows:
        return report

    from src.services.minio_service import MinioService

    minio = await MinioService.get_instance()
    contents = {}
    for row in rows:
        try:
            contents[str(row.id)] = await minio.download_text_async(row.content_path) or ""
        except Exception as e:
            logger.warning(f"Wiki 体检读正文失败 page={row.id}: {e}")
            contents[str(row.id)] = ""

    # index 页目录收录的标题（正文 [[Title]] 链接即目录条目）
    index_row = next((r for r in rows if r.page_type == "index"), None)
    directory_norms = set()
    if index_row is not None:
        directory_norms = {
            normalize_title(t)
            for t in _extract_link_titles(contents.get(str(index_row.id), ""))
        }

    all_active_norms = {normalize_title(r.title) for r in rows}
    # 被其他页 links 指向的标题（排除自引用在比较时处理）
    referenced_norms = {
        normalize_title(t)
        for r in entity_rows
        for t in (r.links or [])
        if normalize_title(t) != normalize_title(r.title)
    }

    for row in entity_rows:
        norm = normalize_title(row.title)
        # 1. 断链：links 指向的 title 无对应 active 页
        for link in row.links or []:
            if normalize_title(link) not in all_active_norms:
                report.issues.append(LintIssue(
                    RULE_BROKEN_LINK, "error", str(row.id), row.title,
                    f"链接目标「{link}」不存在对应 active 页",
                ))
        # 2. 孤立页：不在 index 目录且无其他页链接指向
        if norm not in directory_norms and norm not in referenced_norms:
            report.issues.append(LintIssue(
                RULE_ORPHAN_PAGE, "warning", str(row.id), row.title,
                "未被 index 页目录收录，也没有其他页面链接指向该页",
            ))
        # 3. 目录缺失：未收录进 index 页目录
        if index_row is not None and norm not in directory_norms:
            report.issues.append(LintIssue(
                RULE_MISSING_IN_INDEX, "warning", str(row.id), row.title,
                "未收录进 index 页目录，建议重编译刷新索引页",
            ))
        # 4. 空源页：active 但无来源文档（Phase 2 级联应已防，防御性检查）
        if not (row.source_doc_ids or []):
            report.issues.append(LintIssue(
                RULE_EMPTY_SOURCE, "warning", str(row.id), row.title,
                "页面 active 但来源文档列表为空",
            ))
        # 5. 异常体量
        size = len(contents.get(str(row.id), ""))
        if 0 < size < SIZE_TOO_SHORT:
            report.issues.append(LintIssue(
                RULE_ABNORMAL_SIZE, "info", str(row.id), row.title,
                f"正文仅 {size} 字（<{SIZE_TOO_SHORT}），内容可能不完整",
            ))
        elif size > SIZE_TOO_LONG:
            report.issues.append(LintIssue(
                RULE_ABNORMAL_SIZE, "info", str(row.id), row.title,
                f"正文 {size} 字（>{SIZE_TOO_LONG}），建议拆分页面",
            ))

    return report


def _extract_link_titles(content: str) -> List[str]:
    """提取正文中的 [[Title]] 目录条目（目录规则用，不做存在性校验）。"""
    return [m.strip() for m in _WIKI_LINK_RE.findall(content or "")]
