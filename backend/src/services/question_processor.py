"""问题处理服务。

对用户问题进行重写优化、类型分类与智能推荐问题生成。
"""

import json
import logging
import re

logger = logging.getLogger("rag_system")


class QuestionProcessor:
    """
    问题处理器

    对用户问题进行重写、分类和推荐问题生成。
    """

    def __init__(self, llm):
        """
        初始化问题处理器

        Args:
            llm: LLM 实例
        """
        self.llm = llm

    async def rewrite_question(self, question: str) -> dict:
        """
        重写问题，使其更加清晰、完整

        Args:
            question: 原始问题

        Returns:
            dict: {
                "rewritten": 重写后的问题,
                "original": 原始问题,
                "changes": 改动说明
            }
        """
        template = """
你是一个智能问答助手，擅长优化用户问题表述，使其更加清晰、完整、准确。

原始问题:
{question}

请按照以下要求重写问题：
1. 使问题表述更加清晰准确
2. 补充可能遗漏的关键信息
3. 修正可能的歧义或错误表达
4. 保持原问题的核心意图不变
5. 如果问题已经表达清晰，则保持不变

请以JSON格式返回，格式如下：
{{
    "rewritten": "重写后的问题",
    "changes": "主要改动说明"
}}

请直接返回JSON，不要添加任何解释或其他内容。
"""
        prompt = template.format(question=question)

        try:
            response = await self.llm.ainvoke(prompt)

            # 提取JSON内容
            content = response.content.strip()
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                result = json.loads(json_match.group())
                return {
                    "rewritten": result.get("rewritten", question),
                    "original": question,
                    "changes": result.get("changes", "")
                }
            else:
                return {
                    "rewritten": question,
                    "original": question,
                    "changes": "问题无需修改"
                }
        except Exception as e:
            logger.error(f"问题重写失败: {str(e)}", exc_info=True)
            return {
                "rewritten": question,
                "original": question,
                "changes": "重写失败，保持原问题"
            }

    async def classify_question(self, question: str) -> dict:
        """
        识别问题类型

        Args:
            question: 用户问题

        Returns:
            dict: {
                "type": 问题类型,
                "subtype": 子类型,
                "confidence": 置信度,
                "description": 类型说明
            }
        """
        template = """
你是一个智能问答助手，擅长分析问题类型。

请分析以下问题的类型：

问题: {question}

问题类型定义：
1. 事实性(FACTUAL): 关于客观事实、数据、定义的询问
   - 例如：什么是RAG？2023年有多少用户？
2. 观点性(OPINION): 征求主观意见、建议、评价的询问
   - 例如：你觉得这个方案好吗？如何优化最好？
3. 操作性(OPERATIONAL): 关于如何执行某个操作、步骤的询问
   - 例如：如何上传文档？怎么导出数据？
4. 解释性(EXPLANATORY): 解释原理、原因、过程的询问
   - 例如：为什么会这样？RAG的工作原理是什么？
5. 比较性(COMPARATIVE): 比较不同事物异同的询问
   - 例如：RAG和传统搜索有什么区别？
6. 探索性(EXPLORATORY): 开放性、探索性的询问
   - 例如：关于这个话题还有什么需要了解的？

请以JSON格式返回，格式如下：
{{
    "type": "FACTUAL|OPINION|OPERATIONAL|EXPLANATORY|COMPARATIVE|EXPLORATORY",
    "subtype": "具体子类型",
    "confidence": 0.9,
    "description": "类型说明"
}}

请直接返回JSON，不要添加任何解释或其他内容。
"""
        prompt = template.format(question=question)

        try:
            response = await self.llm.ainvoke(prompt)

            # 提取JSON内容
            content = response.content.strip()
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                result = json.loads(json_match.group())
                return {
                    "type": result.get("type", "EXPLORATORY"),
                    "subtype": result.get("subtype", ""),
                    "confidence": result.get("confidence", 0.5),
                    "description": result.get("description", "")
                }
            else:
                return {
                    "type": "EXPLORATORY",
                    "subtype": "",
                    "confidence": 0.5,
                    "description": "无法确定问题类型"
                }
        except Exception as e:
            logger.error(f"问题分类失败: {str(e)}", exc_info=True)
            return {
                "type": "EXPLORATORY",
                "subtype": "",
                "confidence": 0.5,
                "description": "分类失败"
            }

    async def generate_suggestions(self, question=None, history_context="", kb_ids=None):
        """
        根据上下文生成智能推荐问题

        根据当前问题和历史对话，生成相关的推荐问题，帮助用户探索更多相关内容

        Args:
            question: 当前问题（可选）
            history_context: 历史对话上下文（可选）
            kb_ids: 指定的知识库ID列表（可选）

        Returns:
            list: 推荐问题列表（字符串数组）
        """
        # 构建上下文信息
        context_info = ""

        # 如果有知识库限制，添加相关信息
        if kb_ids and len(kb_ids) > 0:
            context_info += f"当前限定知识库: {len(kb_ids)}个\n"

        # 构建提示词
        template = """
        你是一个智能问答助手，擅长根据上下文生成相关的推荐问题。

        {context_info}

        历史对话:
        {history}

        当前问题:
        {question}

        请基于以上信息，生成5个与当前问题或对话主题相关的推荐问题。要求：
        1. 问题要与当前主题高度相关
        2. 问题要具有探索性和启发性
        3. 问题表述清晰简洁
        4. 不要重复已有的问题

        请直接返回问题列表，每个问题占一行，不要编号。
        """

        prompt = template.format(
            context_info=context_info,
            history=history_context,
            question=question or ""
        )

        try:
            response = await self.llm.ainvoke(prompt)
            suggestions = response.content.strip().split('\n')

            # 清理和过滤推荐问题
            cleaned_suggestions = []
            for suggestion in suggestions:
                suggestion = suggestion.strip()
                # 移除可能的编号前缀（如"1."、"- "等）
                if suggestion.startswith(('1.', '2.', '3.', '4.', '5.', '- ', '* ')):
                    suggestion = suggestion[2:].strip()
                if suggestion and len(suggestion) > 5:
                    cleaned_suggestions.append(suggestion)

            # 确保最多返回5个推荐问题
            return cleaned_suggestions[:5]

        except Exception as e:
            logger.error(f"生成推荐问题失败: {str(e)}", exc_info=True)
            # 返回默认推荐问题
            return [
                "这个话题还有哪些方面可以了解？",
                "相关的常见问题有哪些？",
                "如何应用这些知识？",
                "还有哪些相关资源？",
                "这个概念的核心要点是什么？"
            ]
