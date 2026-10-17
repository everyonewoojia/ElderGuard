"""老人端主入口启动回归测试。

背景：`python src/main.py` 时 sys.path[0] 指向 src 目录，导致 `from src.core.agent` 报
ModuleNotFoundError: No module named 'src'。main.py 顶部已将项目根加入 sys.path。
本测试验证：main 模块可导入、main 协程入口存在（修复后 `python src/main.py` 可正常启动）。
"""
import inspect


def test_main_module_importable():
    """src.main 应能正常导入（不再报 No module named 'src'）。"""
    import src.main
    assert src.main is not None


def test_main_coro_exists():
    """main() 应为可 await 的协程函数（异步主入口）。"""
    import src.main
    assert inspect.iscoroutinefunction(src.main.main)
