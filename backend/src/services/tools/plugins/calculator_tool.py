"""计算器工具插件。

提供安全的数学表达式求值、简单单位换算和日期计算能力，
避免 LLM 在数值计算上出现低级错误。
"""

import ast
import operator as op
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from src.services.tools.tool_manager import BaseTool, ToolResult


# 支持的数学运算符
_ALLOWED_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.USub: op.neg,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
}


class CalculatorTool(BaseTool):
    """安全计算数学表达式、简单单位换算和日期差。"""

    name = "calculator"
    description = (
        "执行安全的数学计算、简单单位换算和日期计算。"
        "当用户问题涉及加减乘除、百分比、单位换算、几天前后日期时使用此工具。"
        "注意：货币汇率使用固定近似值，仅作参考。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的表达式或问题，例如：\"123 * 456\"、\"100 USD to CNY\"、\"3天后是几号\"",
            }
        },
        "required": ["expression"],
    }

    async def execute(self, **kwargs) -> ToolResult:
        expression = kwargs.get("expression", "")
        if not expression:
            return ToolResult(tool_name=self.name, output="请输入需要计算的表达式。")

        # 1. 尝试日期计算
        date_result = self._try_date_calc(expression)
        if date_result:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output=date_result,
            )

        # 2. 尝试单位换算
        unit_result = self._try_unit_conversion(expression)
        if unit_result:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output=unit_result,
            )

        # 3. 数学表达式求值
        math_result = self._safe_eval(expression)
        if math_result is not None:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output=f"计算结果：{math_result}",
            )

        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            success=False,
            error="无法识别该计算请求，请使用标准数学表达式或常见单位换算。",
        )

    def _safe_eval(self, expression: str) -> Optional[str]:
        """安全求值数学表达式，仅支持基本四则运算和幂运算。"""
        # 清理表达式：替换中文符号、删除空格
        cleaned = expression.replace(" ", "").replace("×", "*").replace("÷", "/")
        cleaned = cleaned.replace("（", "(").replace("）", ")")
        cleaned = re.sub(r"[^\d+\-*/().%\s]", "", cleaned)
        if not cleaned:
            return None

        # 处理百分比，如 10% -> 0.1
        cleaned = re.sub(r"(\d+(?:\.\d+)?)%", r"(\1/100)", cleaned)

        try:
            node = ast.parse(cleaned, mode="eval")
            result = self._eval_node(node.body)
            if isinstance(result, (int, float)):
                # 整数尽量显示为整数
                if isinstance(result, float) and result.is_integer():
                    return str(int(result))
                return f"{result:.6f}".rstrip("0").rstrip(".")
            return str(result)
        except Exception:
            return None

    def _eval_node(self, node: ast.AST) -> Any:
        """递归求值 AST 节点。"""
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Unsupported constant")
        if isinstance(node, ast.BinOp):
            operator_type = type(node.op)
            if operator_type not in _ALLOWED_OPERATORS:
                raise ValueError(f"Unsupported operator: {operator_type}")
            return _ALLOWED_OPERATORS[operator_type](
                self._eval_node(node.left), self._eval_node(node.right)
            )
        if isinstance(node, ast.UnaryOp):
            operator_type = type(node.op)
            if operator_type not in _ALLOWED_OPERATORS:
                raise ValueError(f"Unsupported unary operator: {operator_type}")
            return _ALLOWED_OPERATORS[operator_type](self._eval_node(node.operand))
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body)
        raise ValueError("Unsupported expression")

    def _try_unit_conversion(self, expression: str) -> Optional[str]:
        """处理常见单位换算。"""
        text = expression.lower().strip()
        # 货币固定汇率（近似值，仅作参考）
        currency_rates = {
            ("usd", "cny"): 7.25,
            ("cny", "usd"): 0.138,
            ("eur", "cny"): 7.85,
            ("cny", "eur"): 0.127,
            ("jpy", "cny"): 0.046,
            ("cny", "jpy"): 21.7,
            ("gbp", "cny"): 9.20,
            ("cny", "gbp"): 0.109,
        }

        # 匹配 "100 USD to CNY" / "100美元等于多少人民币"
        m = re.search(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]{3}|美元|人民币|欧元|日元|英镑)\s*(?:to|等于|换算成|兑换)\s*([a-zA-Z]{3}|美元|人民币|欧元|日元|英镑)", text)
        if m:
            amount = float(m.group(1))
            src = self._normalize_currency(m.group(2))
            dst = self._normalize_currency(m.group(3))
            rate = currency_rates.get((src, dst))
            if rate:
                result = amount * rate
                return f"{amount} {src.upper()} ≈ {result:.2f} {dst.upper()}（固定参考汇率，非实时）"

        # 长度/重量/温度等简单换算可后续扩展
        return None

    def _normalize_currency(self, name: str) -> str:
        mapping = {
            "usd": "usd", "美元": "usd", "$": "usd",
            "cny": "cny", "人民币": "cny", "￥": "cny", "元": "cny",
            "eur": "eur", "欧元": "eur", "€": "eur",
            "jpy": "jpy", "日元": "jpy", "¥": "jpy",
            "gbp": "gbp", "英镑": "gbp", "£": "gbp",
        }
        return mapping.get(name.lower(), name.lower())

    def _try_date_calc(self, expression: str) -> Optional[str]:
        """处理简单日期计算，如 '3天后是几号'、'10天前'。"""
        text = expression.strip()
        # 匹配 N 天后 / N 天前
        m = re.search(r"(\d+)\s*天([前后])", text)
        if m:
            days = int(m.group(1))
            direction = m.group(2)
            if direction == "前":
                delta = timedelta(days=-days)
            else:
                delta = timedelta(days=days)
            target = datetime.now() + delta
            return f"{days}天{'后' if direction == '后' else '前'}是 {target.strftime('%Y-%m-%d')}（星期{'一二三四五六日'[target.weekday()]})"

        # 匹配 '今天是几号' 这类已由 datetime_tool 处理的问题
        return None
