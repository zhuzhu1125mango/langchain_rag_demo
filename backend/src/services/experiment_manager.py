"""
A/B测试实验管理器

负责实验的创建、管理、流量分配和结果分析
"""

import hashlib
import uuid
from typing import List, Dict, Optional, Any
from sqlalchemy import select, func, text
from src.database import async_session
from src.models.experiment import Experiment, ExperimentVariant, TrafficAllocation, ExperimentMetric, ExperimentResult


class ExperimentManager:
    """
    实验管理器类
    
    提供A/B测试实验的完整生命周期管理
    """
    
    def __init__(self):
        self.active_experiments = {}
    
    async def create_experiment(self, name: str, description: str = None, 
                               variants: List[Dict[str, Any]] = None,
                               metrics: List[str] = None) -> str:
        """
        创建新实验
        
        Args:
            name: 实验名称
            description: 实验描述
            variants: 变体配置列表
            metrics: 监控指标列表
            
        Returns:
            str: 实验ID
        """
        async with async_session() as session:
            experiment = Experiment(
                name=name,
                description=description,
                variants=variants or [],
                metrics=metrics or ["accuracy", "response_time"],
                status="created"
            )
            session.add(experiment)
            await session.commit()
            await session.refresh(experiment)
            
            for variant in variants or []:
                variant_obj = ExperimentVariant(
                    experiment_id=experiment.id,
                    name=variant.get("name", ""),
                    weight=variant.get("weight", 0.5),
                    config=variant.get("config", {}),
                    enabled=variant.get("enabled", True)
                )
                session.add(variant_obj)
            
            await session.commit()
            
            return str(experiment.id)
    
    async def start_experiment(self, experiment_id: str) -> bool:
        """
        启动实验
        
        Args:
            experiment_id: 实验ID
            
        Returns:
            bool: 是否成功启动
        """
        async with async_session() as session:
            result = await session.execute(
                select(Experiment).where(Experiment.id == experiment_id)
            )
            experiment = result.scalar_one_or_none()
            
            if experiment and experiment.status == "created":
                experiment.status = "running"
                experiment.started_at = func.now()
                await session.commit()
                
                self.active_experiments[experiment_id] = experiment
                return True
            
            return False
    
    async def stop_experiment(self, experiment_id: str) -> bool:
        """
        停止实验
        
        Args:
            experiment_id: 实验ID
            
        Returns:
            bool: 是否成功停止
        """
        async with async_session() as session:
            result = await session.execute(
                select(Experiment).where(Experiment.id == experiment_id)
            )
            experiment = result.scalar_one_or_none()
            
            if experiment and experiment.status == "running":
                experiment.status = "stopped"
                experiment.ended_at = func.now()
                await session.commit()
                
                if experiment_id in self.active_experiments:
                    del self.active_experiments[experiment_id]
                
                return True
            
            return False
    
    async def get_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """
        获取实验详情
        
        Args:
            experiment_id: 实验ID
            
        Returns:
            Dict[str, Any] or None: 实验信息
        """
        async with async_session() as session:
            result = await session.execute(
                select(Experiment).where(Experiment.id == experiment_id)
            )
            experiment = result.scalar_one_or_none()
            
            if experiment:
                return {
                    "id": str(experiment.id),
                    "name": experiment.name,
                    "description": experiment.description,
                    "status": experiment.status,
                    "variants": experiment.variants,
                    "metrics": experiment.metrics,
                    "traffic_allocation": experiment.traffic_allocation,
                    "created_at": experiment.created_at.isoformat(),
                    "started_at": experiment.started_at.isoformat() if experiment.started_at else None,
                    "ended_at": experiment.ended_at.isoformat() if experiment.ended_at else None
                }
            
            return None
    
    async def list_experiments(self, status: str = None) -> List[Dict[str, Any]]:
        """
        获取实验列表
        
        Args:
            status: 状态过滤（可选）
            
        Returns:
            List[Dict[str, Any]]: 实验列表
        """
        async with async_session() as session:
            query = select(Experiment)
            if status:
                query = query.where(Experiment.status == status)
            
            result = await session.execute(query)
            experiments = result.scalars().all()
            
            return [
                {
                    "id": str(e.id),
                    "name": e.name,
                    "status": e.status,
                    "created_at": e.created_at.isoformat()
                }
                for e in experiments
            ]
    
    async def delete_experiments(self, experiment_ids: List[str]) -> Dict[str, Any]:
        """
        批量删除实验及其关联数据
        
        Args:
            experiment_ids: 实验ID列表
            
        Returns:
            Dict[str, Any]: 删除结果统计
        """
        if not experiment_ids:
            return {
                "success_count": 0,
                "failed_count": 0,
                "failed": [],
                "message": "未提供实验ID"
            }
        
        valid_ids = []
        failed = []
        
        for raw_id in experiment_ids:
            try:
                valid_ids.append(uuid.UUID(raw_id))
            except ValueError:
                failed.append({
                    "experiment_id": raw_id,
                    "reason": "实验ID格式无效"
                })
        
        async with async_session() as session:
            result = await session.execute(
                select(Experiment).where(Experiment.id.in_(valid_ids))
            )
            experiments = result.scalars().all()
            found_ids = {str(e.id) for e in experiments}
            
            for exp_id in valid_ids:
                if str(exp_id) not in found_ids:
                    failed.append({
                        "experiment_id": str(exp_id),
                        "reason": "实验不存在"
                    })
            
            for experiment in experiments:
                exp_id = str(experiment.id)
                # 从活动实验缓存中移除
                if exp_id in self.active_experiments:
                    del self.active_experiments[exp_id]

                # 显式清理关联数据（兼容无数据库级联约束的旧表结构）
                await session.execute(
                    text("DELETE FROM experiment_variants WHERE experiment_id = :eid"),
                    {"eid": experiment.id}
                )
                await session.execute(
                    text("DELETE FROM traffic_allocations WHERE experiment_id = :eid"),
                    {"eid": experiment.id}
                )
                await session.execute(
                    text("DELETE FROM experiment_metrics WHERE experiment_id = :eid"),
                    {"eid": experiment.id}
                )
                await session.execute(
                    text("DELETE FROM experiment_results WHERE experiment_id = :eid"),
                    {"eid": experiment.id}
                )

                await session.delete(experiment)

            await session.commit()
        
        return {
            "success_count": len(experiments),
            "failed_count": len(failed),
            "failed": failed,
            "message": f"成功删除 {len(experiments)} 个实验，失败 {len(failed)} 个"
        }
    
    def _hash_user_id(self, user_id: str, experiment_id: str) -> float:
        """
        基于用户ID和实验ID计算哈希值
        
        Args:
            user_id: 用户标识
            experiment_id: 实验ID
            
        Returns:
            float: 哈希值（0-1之间）
        """
        combined = f"{user_id}-{experiment_id}"
        hash_value = int(hashlib.md5(combined.encode()).hexdigest(), 16)
        return hash_value / (2**128)
    
    async def allocate_traffic(self, experiment_id: str, user_id: str, 
                              session_id: Optional[str] = None) -> Optional[str]:
        """
        分配用户到变体
        
        Args:
            experiment_id: 实验ID
            user_id: 用户标识
            session_id: 会话ID（可选）
            
        Returns:
            str or None: 分配的变体ID
        """
        if experiment_id not in self.active_experiments:
            experiment = await self.get_experiment(experiment_id)
            if experiment and experiment["status"] == "running":
                self.active_experiments[experiment_id] = experiment
            else:
                return None
        
        experiment = self.active_experiments[experiment_id]
        variants = experiment["variants"]
        
        if not variants:
            return None
        
        hash_value = self._hash_user_id(user_id, experiment_id)
        
        cumulative_weight = 0.0
        for variant in variants:
            cumulative_weight += variant.get("weight", 0.0)
            if hash_value <= cumulative_weight:
                variant_id = variant.get("id")
                
                async with async_session() as session:
                    allocation = TrafficAllocation(
                        experiment_id=experiment_id,
                        variant_id=variant_id,
                        user_id=user_id,
                        session_id=session_id
                    )
                    session.add(allocation)
                    await session.commit()
                
                return variant_id
        
        return None
    
    async def record_metric(self, experiment_id: str, variant_id: str,
                           metric_name: str, metric_value: float):
        """
        记录指标数据
        
        Args:
            experiment_id: 实验ID
            variant_id: 变体ID
            metric_name: 指标名称
            metric_value: 指标值
        """
        async with async_session() as session:
            metric = ExperimentMetric(
                experiment_id=experiment_id,
                variant_id=variant_id,
                metric_name=metric_name,
                metric_value=metric_value
            )
            session.add(metric)
            await session.commit()
    
    async def get_metrics(self, experiment_id: str) -> Dict[str, Dict[str, List[float]]]:
        """
        获取实验指标
        
        Args:
            experiment_id: 实验ID
            
        Returns:
            Dict[str, Dict[str, List[float]]]: 指标数据
        """
        async with async_session() as session:
            result = await session.execute(
                select(ExperimentMetric).where(ExperimentMetric.experiment_id == experiment_id)
            )
            metrics = result.scalars().all()
            
            result_dict = {}
            for metric in metrics:
                variant_id = str(metric.variant_id)
                if variant_id not in result_dict:
                    result_dict[variant_id] = {}
                
                metric_name = metric.metric_name
                if metric_name not in result_dict[variant_id]:
                    result_dict[variant_id][metric_name] = []
                
                result_dict[variant_id][metric_name].append(metric.metric_value)
            
            return result_dict
    
    async def analyze_experiment(self, experiment_id: str) -> Dict[str, Any]:
        """
        分析实验结果

        Args:
            experiment_id: 实验ID

        Returns:
            Dict[str, Any]: 分析结果
        """
        # 验证实验ID是否为有效的UUID格式
        try:
            uuid.UUID(experiment_id)
        except ValueError:
            return {
                "experiment_id": experiment_id,
                "status": "no_data",
                "message": "暂无指标数据"
            }

        metrics = await self.get_metrics(experiment_id)

        if not metrics:
            return {
                "experiment_id": experiment_id,
                "status": "no_data",
                "message": "暂无指标数据"
            }

        # 获取实验信息以映射变体名称
        experiment = await self.get_experiment(experiment_id)
        variant_name_map = {}
        if experiment and experiment.get("variants"):
            for v in experiment["variants"]:
                variant_name_map[str(v.get("id", ""))] = v.get("name", "")

        variant_results = []
        winning_variant = None
        best_metric_value = float('-inf')

        for variant_id, variant_metrics in metrics.items():
            result_metrics = {}
            sample_size = 0

            for metric_name, values in variant_metrics.items():
                if not values:
                    continue
                avg_value = sum(values) / len(values)
                variance = sum((x - avg_value) ** 2 for x in values) / len(values)
                std = variance ** 0.5

                result_metrics[metric_name] = {
                    "mean": avg_value,
                    "std": std,
                    "count": len(values)
                }
                sample_size = max(sample_size, len(values))

                if metric_name == "accuracy" and avg_value > best_metric_value:
                    best_metric_value = avg_value
                    winning_variant = variant_id

            variant_results.append({
                "variant_id": variant_id,
                "variant_name": variant_name_map.get(variant_id, variant_id),
                "sample_size": sample_size,
                "metrics": result_metrics
            })

        result_data = {
            "experiment_id": experiment_id,
            "status": "completed",
            "variant_results": variant_results,
            "winner_variant_id": winning_variant,
            "confidence_level": 0.85 if winning_variant else 0.0
        }

        async with async_session() as session:
            result = ExperimentResult(
                experiment_id=experiment_id,
                winning_variant_id=winning_variant,
                analysis_data=result_data,
                confidence=0.85 if winning_variant else 0.0,
                conclusion=f"获胜变体: {winning_variant}" if winning_variant else "无法确定获胜变体"
            )
            session.add(result)
            await session.commit()

        return result_data
    
    async def get_experiment_result(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """
        获取实验分析结果

        Args:
            experiment_id: 实验ID

        Returns:
            Dict[str, Any] or None: 分析结果
        """
        async with async_session() as session:
            result = await session.execute(
                select(ExperimentResult).where(ExperimentResult.experiment_id == experiment_id)
            )
            result_obj = result.scalar_one_or_none()

            if result_obj:
                analysis_data = result_obj.analysis_data or {}
                # 优先使用已存储的新格式数据
                if "variant_results" in analysis_data:
                    return {
                        "experiment_id": str(result_obj.experiment_id),
                        "variant_results": analysis_data["variant_results"],
                        "winner_variant_id": str(result_obj.winning_variant_id) if result_obj.winning_variant_id else None,
                        "confidence_level": result_obj.confidence,
                        "analyzed_at": result_obj.analyzed_at.isoformat()
                    }
                # 兼容旧格式转换
                variant_results = []
                for variant_id, metrics in analysis_data.items():
                    result_metrics = {}
                    sample_size = 0
                    for metric_name, stat in metrics.items():
                        if isinstance(stat, dict):
                            result_metrics[metric_name] = {
                                "mean": stat.get("average", 0),
                                "std": 0.0,
                                "count": stat.get("count", 0)
                            }
                            sample_size = max(sample_size, stat.get("count", 0))
                    variant_results.append({
                        "variant_id": variant_id,
                        "variant_name": variant_id,
                        "sample_size": sample_size,
                        "metrics": result_metrics
                    })
                return {
                    "experiment_id": str(result_obj.experiment_id),
                    "variant_results": variant_results,
                    "winner_variant_id": str(result_obj.winning_variant_id) if result_obj.winning_variant_id else None,
                    "confidence_level": result_obj.confidence,
                    "analyzed_at": result_obj.analyzed_at.isoformat()
                }

            return None


experiment_manager = ExperimentManager()
