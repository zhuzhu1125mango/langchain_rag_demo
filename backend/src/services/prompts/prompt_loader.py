"""提示词加载器。

从 backend/src/prompts/ 目录加载文本模板，并支持变量替换。
"""

import os
from typing import Optional

import aiofiles


class PromptLoader:
    """提示词加载器。"""

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            # 默认路径：backend/src/prompts
            current_file = os.path.abspath(__file__)
            base_dir = os.path.join(os.path.dirname(os.path.dirname(current_file)), "prompts")
        self.base_dir = base_dir

    async def load(self, name: str, **variables) -> str:
        """加载指定名称的提示词模板并替换变量。

        Args:
            name: 模板文件名（不含 .txt 后缀）。
            **variables: 模板变量。

        Returns:
            替换变量后的提示词文本。
        """
        path = os.path.join(self.base_dir, f"{name}.txt")
        async with aiofiles.open(path, "r", encoding="utf-8") as f:
            template = await f.read()
        return template.format(**variables)

    def exists(self, name: str) -> bool:
        """判断模板文件是否存在。"""
        path = os.path.join(self.base_dir, f"{name}.txt")
        return os.path.isfile(path)
