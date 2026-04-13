"""
预约流程入口 - 

该模块现在作为 main.py 的兼容适配层，
实际预约逻辑已迁移至 agent.py 的智能 Agent 循环体。

保留 BookingError 和 run_booking 签名以兼容 main.py 的现有调用。
"""
import logging
from typing import Callable
import agent  # 导入智能 Agent 模块

logger = logging.getLogger(__name__)

# 向后兼容：从 agent 模块再导出 BookingError，
# 这样 main.py 的 `except booking_flow.BookingError` 继续生效
BookingError = agent.BookingError


def run_booking(
    hwnd: int,
    dry_run: bool = False,
    start_time: str | None = None,
    end_time: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> bool:
    """
    执行完整的自习室预约流程（委托给 agent.run_agent）。

    Args:
        hwnd:       已锁定的目标窗口句柄
        dry_run:    测试模式（True 时不点击最末「确定」按钮）
        start_time: 预约开始时间字符串（如 "8:00"），None 则读 config
        end_time:   预约结束时间字符串（如 "22:00"），None 则读 config
        should_cancel: 外部停止检查函数，返回 True 时中断任务

    Returns:
        bool: 是否成功完成预约
    """
    logger.info("[booking_flow] 委托给 Agent 智能循环执行...")
    return agent.run_agent(
        hwnd=hwnd,
        dry_run=dry_run,
        start_time=start_time,
        end_time=end_time,
        should_cancel=should_cancel,
    )
