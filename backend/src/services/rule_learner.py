"""
规则学习器 - RuleLearner

基于用户反馈训练分类模型，优化策略权重和规则
支持误分类检测和模式归纳
"""

from typing import List, Dict, Optional, Any, Tuple
import numpy as np
from collections import defaultdict
import re
from .sample_store import sample_store


class RuleLearner:
    """
    规则学习器类
    
    基于历史执行记录和用户反馈，动态优化策略权重和阈值
    支持误分类检测和模式归纳
    """
    
    def __init__(self):
        self.min_samples_for_training = 50
        self.learning_rate = 0.1
        self.confidence_threshold = 0.7
        self.pattern_min_frequency = 3
    
    async def extract_training_samples(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """
        提取训练样本
        
        Args:
            limit: 样本数量限制
            
        Returns:
            List[Dict[str, Any]]: 训练样本列表
        """
        executions = await sample_store.get_executions_with_feedback(limit)
        samples = []
        
        for execution in executions:
            feedbacks = execution.get("feedbacks", [])
            
            if feedbacks:
                avg_feedback = sum(f["feedback_score"] for f in feedbacks) / len(feedbacks)
                label = 1 if avg_feedback > 0 else 0
            else:
                label = None
            
            samples.append({
                "question": execution["question"],
                "decision": execution["final_decision"],
                "confidence": execution["final_confidence"],
                "strategy_results": execution["strategy_results"],
                "label": label
            })
        
        return [s for s in samples if s["label"] is not None]
    
    def calculate_strategy_accuracy(self, samples: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        计算各策略的准确率
        
        Args:
            samples: 训练样本列表
            
        Returns:
            Dict[str, float]: 各策略准确率
        """
        strategy_correct = defaultdict(int)
        strategy_total = defaultdict(int)
        
        for sample in samples:
            strategy_results = sample.get("strategy_results", {})
            actual_label = sample["label"]
            
            for strategy_name, result in strategy_results.items():
                strategy_total[strategy_name] += 1
                predicted_label = 1 if result > 0.5 else 0
                if predicted_label == actual_label:
                    strategy_correct[strategy_name] += 1
        
        accuracy = {}
        for strategy_name, total in strategy_total.items():
            if total > 0:
                accuracy[strategy_name] = strategy_correct[strategy_name] / total
            else:
                accuracy[strategy_name] = 0.5
        
        return accuracy
    
    def train_weight_adjustment(self, samples: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        训练权重调整
        
        Args:
            samples: 训练样本列表
            
        Returns:
            Dict[str, float]: 各策略的权重调整量
        """
        if len(samples) < self.min_samples_for_training:
            return {}
        
        accuracy = self.calculate_strategy_accuracy(samples)
        adjustments = {}
        
        avg_accuracy = sum(accuracy.values()) / len(accuracy) if accuracy else 0.5
        
        for strategy_name, acc in accuracy.items():
            if acc > avg_accuracy:
                adjustments[strategy_name] = self.learning_rate * (acc - avg_accuracy)
            else:
                adjustments[strategy_name] = -self.learning_rate * (avg_accuracy - acc)
        
        return adjustments
    
    async def learn_and_update(self, strategy_manager) -> Dict[str, Any]:
        """
        执行学习并更新策略
        
        Args:
            strategy_manager: 策略管理器实例
            
        Returns:
            Dict[str, Any]: 学习结果
        """
        samples = await self.extract_training_samples()
        
        if len(samples) < self.min_samples_for_training:
            return {
                "status": "skipped",
                "reason": f"样本数量不足，需要至少 {self.min_samples_for_training} 个标注样本",
                "samples_available": len(samples)
            }
        
        adjustments = self.train_weight_adjustment(samples)
        
        if adjustments:
            strategy_manager.adjust_weights(adjustments)
            
            for strategy_name, adjustment in adjustments.items():
                await sample_store.update_strategy_config(
                    strategy_name,
                    {"weight": strategy_manager.weights.get(strategy_name, 0.25)}
                )
        
        accuracy = self.calculate_strategy_accuracy(samples)
        
        return {
            "status": "success",
            "samples_used": len(samples),
            "accuracy": accuracy,
            "adjustments": adjustments,
            "new_weights": strategy_manager.weights.copy()
        }
    
    async def analyze_misclassifications(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        分析误分类案例
        
        Args:
            limit: 返回数量限制
            
        Returns:
            List[Dict[str, Any]]: 误分类案例列表
        """
        samples = await self.extract_training_samples()
        misclassified = []
        
        for sample in samples[:limit]:
            strategy_results = sample.get("strategy_results", {})
            actual_label = sample["label"]
            
            for strategy_name, result in strategy_results.items():
                predicted_label = 1 if result > 0.5 else 0
                if predicted_label != actual_label:
                    misclassified.append({
                        "question": sample["question"],
                        "strategy_name": strategy_name,
                        "predicted": predicted_label,
                        "actual": actual_label,
                        "confidence": result
                    })
        
        return misclassified
    
    def _extract_ngrams(self, text: str, n: int = 2) -> List[str]:
        """
        提取文本中的n-gram特征
        
        Args:
            text: 输入文本
            n: n-gram的n值
            
        Returns:
            List[str]: n-gram列表
        """
        words = text.split()
        ngrams = []
        for i in range(len(words) - n + 1):
            ngrams.append(" ".join(words[i:i+n]))
        return ngrams
    
    def _find_common_patterns(self, questions: List[str]) -> List[Tuple[str, int, float]]:
        """
        从问题列表中发现常见模式
        
        Args:
            questions: 问题列表
            
        Returns:
            List[Tuple[str, int, float]]: 模式列表 (模式, 频率, 覆盖率)
        """
        if len(questions) < self.pattern_min_frequency:
            return []
        
        pattern_counts = defaultdict(int)
        
        for question in questions:
            question = question.lower().strip()
            
            patterns = []
            
            patterns.extend(self._extract_ngrams(question, 1))
            patterns.extend(self._extract_ngrams(question, 2))
            
            for pattern in patterns:
                pattern_counts[pattern] += 1
        
        total_questions = len(questions)
        sorted_patterns = sorted(
            pattern_counts.items(), 
            key=lambda x: x[1], 
            reverse=True
        )
        
        result = []
        for pattern, count in sorted_patterns:
            if count >= self.pattern_min_frequency:
                coverage = count / total_questions
                result.append((pattern, count, coverage))
        
        return result[:20]
    
    async def suggest_new_keywords(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        基于误分类案例建议新关键词
        
        Args:
            limit: 返回数量限制
            
        Returns:
            List[Dict[str, Any]]: 建议的关键词列表
        """
        misclassified = await self.analyze_misclassifications(limit * 10)
        keywords = defaultdict(int)
        
        for case in misclassified:
            question = case["question"]
            words = question.split()
            
            for word in words:
                if len(word) >= 2:
                    keywords[word] += 1
        
        sorted_keywords = sorted(keywords.items(), key=lambda x: x[1], reverse=True)
        
        return [
            {"keyword": kw, "frequency": freq}
            for kw, freq in sorted_keywords[:limit]
        ]
    
    async def discover_question_patterns(self, limit: int = 50) -> Dict[str, Any]:
        """
        从误分类案例中发现问题模式
        
        Args:
            limit: 返回数量限制
            
        Returns:
            Dict[str, Any]: 模式分析结果
        """
        misclassified = await self.analyze_misclassifications(limit * 10)
        
        false_positive_questions = [
            case["question"] for case in misclassified 
            if case["predicted"] == 1 and case["actual"] == 0
        ]
        
        false_negative_questions = [
            case["question"] for case in misclassified 
            if case["predicted"] == 0 and case["actual"] == 1
        ]
        
        fp_patterns = self._find_common_patterns(false_positive_questions)
        fn_patterns = self._find_common_patterns(false_negative_questions)
        
        return {
            "false_positive_patterns": [
                {"pattern": p[0], "frequency": p[1], "coverage": round(p[2], 3)}
                for p in fp_patterns
            ],
            "false_negative_patterns": [
                {"pattern": p[0], "frequency": p[1], "coverage": round(p[2], 3)}
                for p in fn_patterns
            ],
            "false_positive_count": len(false_positive_questions),
            "false_negative_count": len(false_negative_questions),
            "total_misclassified": len(misclassified)
        }
    
    async def update_threshold_based_on_feedback(self, strategy_manager) -> Dict[str, float]:
        """
        根据反馈更新决策阈值
        
        Args:
            strategy_manager: 策略管理器实例
            
        Returns:
            Dict[str, float]: 更新结果
        """
        samples = await self.extract_training_samples()
        
        if len(samples) < self.min_samples_for_training:
            return {"status": "skipped", "reason": "样本数量不足"}
        
        confidences = [s["confidence"] for s in samples if s["label"] == 1]
        avg_positive_confidence = sum(confidences) / len(confidences) if confidences else 0.5
        
        new_threshold = min(0.9, max(0.1, avg_positive_confidence - 0.1))
        old_threshold = strategy_manager.threshold
        strategy_manager.set_threshold(new_threshold)
        
        return {
            "status": "success",
            "old_threshold": old_threshold,
            "new_threshold": new_threshold,
            "samples_analyzed": len(samples)
        }
    
    async def learn_intent_classifier(
        self,
        classifier,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """
        基于历史执行反馈在线学习意图分类器示例库。
        
        将 strategy_executions 中带有用户反馈的记录转换为意图分类样本：
        - 反馈为正（label=1）→ 视为 actual_mode = kb_only（知识库意图被正确触发）
        - 反馈为负（label=0）→ 视为 actual_mode = direct_llm（不应使用知识库）
        
        调用 classifier.learn_from_feedback 将正确样本加入对应意图示例库。
        
        Args:
            classifier: EmbeddingIntentClassifier 实例或具有 learn_from_feedback 方法的对象
            limit: 最多处理的记录数
            
        Returns:
            Dict[str, Any]: 学习统计信息
        """
        samples = await self.extract_training_samples(limit)
        if len(samples) < 1:
            return {
                "status": "skipped",
                "reason": "无可用反馈样本",
                "samples_available": 0,
            }
        
        learned = 0
        correct = 0
        skipped = 0
        for sample in samples:
            question = sample.get("question", "")
            if not question:
                skipped += 1
                continue
            
            label = sample.get("label")
            actual_mode = "kb_only" if label == 1 else "direct_llm"
            
            try:
                # 获取分类器对当前问题的预测结果
                result = await classifier.classify(question)
                predicted_mode = result.primary_mode.value
                confidence = result.confidence
            except Exception as e:
                skipped += 1
                continue
            
            learn_result = await classifier.learn_from_feedback(
                question=question,
                predicted_mode=predicted_mode,
                actual_mode=actual_mode,
                confidence=confidence,
            )
            
            if learn_result["status"] == "misclassification_learned":
                learned += 1
            else:
                correct += 1
        
        return {
            "status": "success",
            "samples_used": len(samples),
            "learned": learned,
            "correct": correct,
            "skipped": skipped,
        }


rule_learner = RuleLearner()
