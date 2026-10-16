"""
elder-agent 测试基座
- 保证 `src` 包可被 tests 导入（pythonpath=. 兜底）
- 提供临时数据目录 fixture，避免污染开发期记忆/缓存
"""
import sys
from pathlib import Path

# 将项目根目录（elder-agent）加入 sys.path，保证 `import src.*` 可用
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture()
def tmp_data_dir(tmp_path):
    """返回一个临时目录路径，供记忆/缓存等持久化测试使用"""
    return tmp_path
