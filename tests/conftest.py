"""
测试全局配置

统一把项目根目录加入 sys.path，保证 `pytest` 在任意工作目录下都能导入 app 包。
同时提供跨测试复用的通用 fixture。
"""

import sys
from pathlib import Path

import pytest

# 项目根目录（tests/ 的上一级）加入模块搜索路径
PROJECT_ROOT = Path(__file__).parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def project_root() -> Path:
    """返回项目根目录，供需要读取 prompts/conf 的测试使用"""
    return PROJECT_ROOT
