"""
敏感词过滤器 - SensitiveWordFilter

本模块提供敏感词检测和过滤功能，用于：
1. 检测用户输入中的敏感内容
2. 对敏感内容进行脱敏处理

支持的功能：
- 敏感词检测（返回是否包含敏感词及具体敏感词）
- 敏感词过滤（将敏感词替换为*）
- 支持敏感词类别查询
"""

class SensitiveWordFilter:
    """
    敏感词过滤器类
    
    实现敏感词的检测和过滤功能
    """
    
    def __init__(self):
        """初始化敏感词过滤器，加载敏感词库"""
        self.sensitive_words, self.category_map = self._load_sensitive_words()
    
    def _load_sensitive_words(self):
        """
        加载敏感词库，按类别分组
        
        Returns:
            tuple: (sensitive_words, category_map)
                - sensitive_words: 敏感词列表
                - category_map: 敏感词到类别的映射字典
        """
        # 敏感词分类
        categories = {
            "政治敏感": [
                "敏感", "敏感词", "领导人", "政治", "政府", "政党",
                "反革命", "颠覆", "分裂", "台独", "港独", "藏独", "疆独",
                "邪教", "法轮功", "六四", "天安门", "中南海", "国务院",
                "习近平", "李克强", "胡锦涛", "温家宝", "江泽民", "毛泽东",
                "邓小平", "李克强", "栗战书", "汪洋", "王沪宁", "赵乐际",
                "韩正", "孙春兰", "胡春华", "黄坤明", "刘鹤", "魏凤和",
                "王勇", "王毅", "肖捷", "赵克志", "周强", "张军",
                "中央委员会", "中央政治局", "中央军委", "全国人大", "全国政协"
            ],
            "色情低俗": [
                "色情", "情色", "黄色", "裸体", "裸照", "露点",
                "性爱", "性交", "做爱", "手淫", "嫖娼", "卖淫",
                "妓女", "小姐", "AV", "三级片", "成人", "性服务",
                "啪啪", "约炮", "炮友", "援交", "SM", "捆绑",
                "制服诱惑", "丝袜", "爆乳", "巨乳", "萝莉", "御姐",
                "人妻", "乱伦", "强奸", "轮奸", "奸淫", "猥亵"
            ],
            "暴力恐怖": [
                "暴力", "恐怖", "杀人", "自杀", "自残", "割腕",
                "跳楼", "爆炸", "炸弹", "枪击", "暗杀", "绑架",
                "抢劫", "盗窃", "诈骗", "勒索", "纵火", "投毒",
                "刀具", "枪支", "弹药", "武器", "攻击", "殴打",
                "黑社会", "黑帮", "贩毒", "制毒", "走私", "偷渡"
            ],
            "毒品相关": [
                "毒品", "鸦片", "海洛因", "冰毒", "大麻", "可卡因",
                "摇头丸", "K粉", "吗啡", "杜冷丁", "安非他命", "兴奋剂",
                "戒毒", "贩毒", "吸毒", "制毒", "走私毒品", "新型毒品"
            ],
            "赌博相关": [
                "赌博", "赌球", "赌马", "六合彩", "彩票", "博彩",
                "百家乐", "德州扑克", "麻将", "斗地主", "炸金花", "斗牛",
                "网上赌博", "境外赌博", "赌资", "赌徒", "赌场", "赌局"
            ],
            "违法犯罪": [
                "违法", "违规", "犯罪", "判刑", "坐牢", "监狱",
                "通缉", "逮捕", "拘留", "罚款", "审判", "法庭",
                "律师", "法官", "检察官", "警察", "派出所", "公安局",
                "走私", "偷税", "漏税", "贪污", "受贿", "挪用公款",
                "洗钱", "非法集资", "传销", "诈骗", "欺诈", "假冒伪劣"
            ],
            "其他违规": [
                "广告", "推广", "营销", "引流", "刷单", "刷量",
                "作弊", "抄袭", "侵权", "盗版", "色情广告", "垃圾信息",
                "恶意攻击", "人身攻击", "辱骂", "诽谤", "造谣", "传谣"
            ]
        }
        
        # 扁平化敏感词列表
        sensitive_words = []
        category_map = {}
        
        for category, words in categories.items():
            for word in words:
                sensitive_words.append(word)
                category_map[word] = category
        
        return sensitive_words, category_map
    
    def detect(self, text):
        """
        检测文本中是否包含敏感词
        
        Args:
            text: 待检测文本
            
        Returns:
            tuple: (is_sensitive, sensitive_word, category)
                - is_sensitive: 是否包含敏感词
                - sensitive_word: 检测到的敏感词（若无则为None）
                - category: 敏感词类别（若无则为None）
        """
        for word in self.sensitive_words:
            if word in text:
                return True, word, self.category_map.get(word, "其他")
        return False, None, None
    
    def filter(self, text):
        """
        过滤文本中的敏感词（替换为*）
        
        Args:
            text: 待过滤文本
            
        Returns:
            str: 过滤后的文本
        """
        for word in self.sensitive_words:
            text = text.replace(word, '*' * len(word))
        return text
    
    def get_categories(self):
        """
        获取所有敏感词类别
        
        Returns:
            list: 类别名称列表
        """
        return list(set(self.category_map.values()))
    
    def get_words_by_category(self, category):
        """
        获取指定类别的敏感词
        
        Args:
            category: 类别名称
            
        Returns:
            list: 该类别的敏感词列表
        """
        return [word for word, cat in self.category_map.items() if cat == category]
    
    def get_word_count(self):
        """
        获取敏感词总数
        
        Returns:
            int: 敏感词总数
        """
        return len(self.sensitive_words)

# 创建全局敏感词过滤器实例
sensitive_filter = SensitiveWordFilter()