"""意图示例库。

为基于 Embedding 的意图分类器提供预定义示例问题。
每个主模式对应一组代表性问法，支持运行时扩展与在线学习更新。
"""

from typing import Dict, List

from src.services.intent_router.models import PrimaryMode


DEFAULT_INTENT_EXAMPLES: Dict[str, List[str]] = {
    PrimaryMode.DIRECT_LLM.value: [
        "你好",
        "早上好",
        "晚上好",
        "你是谁",
        "你能做什么",
        "介绍一下自己",
        "谢谢",
        "再见",
        "拜拜",
        "hello",
        "hi",
        "在吗",
        "你今天怎么样",
        "讲个笑话",
        "给我背首诗",
        "用一句话总结人工智能",
        "什么是生命的意义",
        "如何保持健康",
        "帮我写一段祝福语",
        "翻译成英文：你好世界",
    ],
    PrimaryMode.KB_ONLY.value: [
        "员工请假制度是怎么规定的",
        "公司报销流程是什么",
        "给我看一下项目文档",
        "根据制度第三条如何处理",
        "这份手册里写了什么",
        "请查阅相关资料",
        "文件里怎么说的",
        "公司考勤制度第几条",
        "技术文档中有相关说明吗",
        "按照规范应该怎么操作",
        "帮我找出制度依据",
        "这份资料的要点是什么",
        "内部流程图在哪里",
        "请引用文档内容回答",
        "知识库里有相关案例吗",
    ],
    PrimaryMode.TOOL_FIRST.value: [
        "现在几点",
        "今天星期几",
        "北京天气怎么样",
        "上海今天会下雨吗",
        "杭州气温多少度",
        "1 加 1 等于几",
        "100 美元等于多少人民币",
        "今天黄金价格多少",
        "白银多少钱一克",
        "美元兑人民币汇率",
        "计算 25 乘以 4",
        "深圳明天的空气质量",
        "现在金价是多少",
        "欧元兑换人民币",
        "帮我算一下房贷月供",
    ],
    PrimaryMode.WEB_SEARCH.value: [
        "今天有什么新闻",
        "最新的科技动态",
        "现在比特币价格",
        "今天金价走势",
        "最新电影票房",
        "今天的股市行情",
        "最近有什么热门事件",
        "现在火箭发射了吗",
        "今日天气预警",
        "最新政策解读",
        "当前的国际油价",
        "今天比赛结果",
        "现在疫情数据如何",
        "最新手机评测",
        "今天汇率是多少",
    ],
    PrimaryMode.AGENT_RESEARCH.value: [
        "对比一下 iPhone 和华为手机的优缺点",
        "帮我调研一下新能源汽车市场",
        "分析一下中美 AI 发展的差异",
        "总结一下 2024 年大模型进展",
        "比较 Python 和 Java 的区别",
        "帮我研究一下可再生能源趋势",
        "有哪些适合初学者的编程语言",
        "分析一下全球半导体产业格局",
        "调研一下国内电商平台的竞争情况",
        "总结一下云计算的优缺点",
        "对比 MacBook 和 ThinkPad",
        "帮我分析远程办公的利弊",
        "研究一下光伏行业的发展前景",
        "对比几个主流数据库",
        "总结一下 DevOps 最佳实践",
    ],
    PrimaryMode.HYBRID.value: [
        "结合公司制度和最新法规，这个合规吗",
        "根据内部文档和最新新闻，分析一下这个项目",
        "用知识库资料加联网信息回答",
        "内部制度和外部标准有什么差异",
        "参考公司手册和最新行业动态",
        "把文档内容和实时信息结合起来看",
        "内部规定和最新政策冲突吗",
        "结合资料和网络信息说明一下",
        "根据知识库和最新报道总结",
        "用内部文件和外部新闻对比分析",
    ],
}


class IntentExampleStore:
    """意图示例存储器，支持默认示例加载、运行时扩展与按模式查询。"""

    def __init__(self, examples: Dict[str, List[str]] | None = None):
        self._examples: Dict[str, List[str]] = {
            mode.value: [] for mode in PrimaryMode
        }
        source = examples or DEFAULT_INTENT_EXAMPLES
        for mode, texts in source.items():
            self.add_examples(mode, texts)

    def add_examples(self, mode: str, texts: List[str]) -> None:
        """向指定模式追加示例，自动去重并过滤空字符串。"""
        if mode not in self._examples:
            self._examples[mode] = []
        existing = set(self._examples[mode])
        for text in texts:
            text = (text or "").strip()
            if text and text not in existing:
                self._examples[mode].append(text)
                existing.add(text)

    def get_examples(self, mode: str | None = None) -> Dict[str, List[str]] | List[str]:
        """获取示例。

        Args:
            mode: 主模式值。为 None 时返回全部示例字典。

        Returns:
            指定模式的示例列表，或全部示例字典。
        """
        if mode is None:
            return {k: list(v) for k, v in self._examples.items()}
        return list(self._examples.get(mode, []))

    def remove_examples(self, mode: str, texts: List[str]) -> None:
        """从指定模式移除示例。"""
        if mode not in self._examples:
            return
        targets = {t.strip() for t in texts}
        self._examples[mode] = [t for t in self._examples[mode] if t not in targets]

    def list_modes(self) -> List[str]:
        """返回所有已定义的模式值列表。"""
        return list(self._examples.keys())


# 全局默认示例库实例
intent_example_store = IntentExampleStore()
