"""
智能 Agent 模块 - 用「感知-推理-行动」循环替代硬编码步骤。

核心流程：
    截图 → AI 判断当前界面状态 → AI 选择工具（click/scroll/wait）→ 执行 → 再截图 → ...

提示词读取优先级：
    1) 用户自定义文件 user_system_promote.txt（用户在 GUI 点击保存后生成）
    2) 默认文件 default_system_prompt.txt（建议通过 Git 同步维护）
    3) 不提供代码兜底，文件缺失时直接报错
"""
import os
import io
import re
import json
import math
import time
import base64
import logging
from datetime import datetime
from typing import Callable
from PIL import Image, ImageChops
from openai import OpenAI

import config
import window_ctrl

logger = logging.getLogger(__name__)

# 单次滚动限幅：避免一次滚动过多导致时间选项越过目标。
SCROLL_MAX_CLICKS_GENERAL = 8
SCROLL_MAX_CLICKS_TIME_DROPDOWN = 4
SCROLL_NO_CHANGE_RATIO_THRESHOLD = 0.003
SCROLL_REASON_TIME_KEYWORDS = (
    "时间", "开始时间", "结束时间", "下拉", "列表", "选时", "选时段",
    "time", "dropdown", "picker", "clock",
)

# ============================================================
# 提示词模板与持久化
# ============================================================

# 注意：提示词不再在代码中硬编码。
# default_system_prompt.txt 是唯一默认来源，缺失时将直接报错。


# 提示词文件路径（与代码同目录）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认提示词来源（建议通过 Git 同步维护）
DEFAULT_PROMPT_FILE = os.path.join(_BASE_DIR, "default_system_prompt.txt")

# 用户手动保存后的提示词（存在时优先使用）
USER_PROMPT_FILE = os.path.join(_BASE_DIR, "user_system_promote.txt")

# 兼容旧代码中对 PROMPT_FILE 的引用（指向用户提示词文件）
PROMPT_FILE = USER_PROMPT_FILE


def _read_prompt_file(file_path: str) -> str | None:
    """读取提示词文件，返回去首尾空白后的文本；读取失败或为空返回 None。"""
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return content or None
    except Exception as e:
        logger.warning(f"读取提示词文件失败: {file_path}，错误: {e}")
        return None


def _build_missing_prompt_error(file_path: str, role: str) -> str:
    """构建提示词文件缺失/无效时的统一报错文案。"""
    filename = os.path.basename(file_path)
    return (
        f"{role}提示词文件不可用：{filename}。"
        "请重新拉取 Git 仓库或重新下载完整项目后再试。"
    )


def load_default_prompt() -> str:
    """加载默认提示词：仅允许从默认文件读取，失败时直接抛错。"""
    content = _read_prompt_file(DEFAULT_PROMPT_FILE)
    if content:
        logger.info("已从 default_system_prompt.txt 加载默认提示词")
        return content

    msg = _build_missing_prompt_error(DEFAULT_PROMPT_FILE, "默认")
    logger.error(msg)
    raise FileNotFoundError(msg)


def load_prompt() -> str:
    """加载提示词：优先用户提示词；不存在则读取默认提示词；不可用时直接抛错。"""
    user_content = _read_prompt_file(USER_PROMPT_FILE)
    if user_content:
        logger.info("已从 user_system_promote.txt 加载用户提示词")
        return user_content

    return load_default_prompt()


def save_prompt(text: str) -> bool:
    """将提示词保存到 user_system_promote.txt（用户优先提示词文件）。"""
    try:
        with open(USER_PROMPT_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info("提示词已保存到 user_system_promote.txt，后续将优先使用用户提示词")
        return True
    except Exception as e:
        logger.error(f"保存提示词失败: {e}")
        return False


# ============================================================
# Tool 定义（OpenAI function-calling 格式）
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": (
                "在界面上的指定归一化坐标（0-1000 范围，左上角为原点）处点击一次。"
                "点击后界面通常需要一段时间响应，请配合 wait_and_capture 等待结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "归一化 x 坐标（0-1000）"},
                    "y": {"type": "integer", "description": "归一化 y 坐标（0-1000）"},
                    "reason": {"type": "string", "description": "点击原因的简要说明（用于日志）"},
                },
                "required": ["x", "y", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "向上或向下滚动界面，用于在座位图中查找当前视野外的座位。",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "滚动方向：up=向上，down=向下",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "滚动格数（建议 1-6；时间下拉列表建议 1-3），默认 3",
                        "default": 3,
                    },
                    "reason": {"type": "string", "description": "滚动原因"},
                },
                "required": ["direction", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_and_capture",
            "description": (
                "等待指定秒数后重新截图并返回给 AI，用于等待界面响应（弹窗出现、页面跳转等）。"
                "调用后 AI 将在下一轮看到最新截图。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "等待秒数（建议 0.5-3.0），默认 1.5",
                        "default": 1.5,
                    },
                    "reason": {"type": "string", "description": "等待原因说明"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "预约任务已成功完成时调用此工具（即确认按钮已点击，界面显示成功）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "完成说明（如：已点击确定，预约已提交）"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_failed",
            "description": "任务无法继续完成时调用（如座位全满、时间选项不存在、重复失败等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "失败原因（用于报告给用户）"},
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "获取当前北京时间（中国标准时间 CST，UTC+8），精确到日、时、分。"
                "当时间选择下拉列表中出现\"现在\"等动态选项，"
                "或需要判断当前时刻与所选时段是否冲突时，调用此工具确认准确时间。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user_for_help",
            "description": (
                "当连续多次尝试依然无法确定当前界面状态或找不到下一步操作目标时调用。"
                "该工具会弹出一个输入框询问大屏前的人类用户，获取指导建议"
                "（如明确指出界面上某个元素的方位，或告知发生了什么意外情况）。"
                "请清楚描述你的疑问和遇到困难的原因。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "你想询问用户的具体问题"},
                    "reason": {"type": "string", "description": "为什么需要求助（遇到了什么困难）"},
                },
                "required": ["question", "reason"],
            },
        },
    },
]


# ============================================================
# 工具执行器
# ============================================================

def _image_to_base64(img: Image.Image) -> str:
    """将 PIL Image 转为 JPEG base64 字符串（节省 Token）。"""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _save_screenshot(img: Image.Image, label: str):
    """保存截图到 screenshots/ 目录，用于调试回溯。"""
    os.makedirs("screenshots", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = os.path.join("screenshots", f"{ts}_{label}.jpg")
    img.save(path, "JPEG", quality=85)
    logger.info(f"截图保存: {path}")


def _normalize_to_pixel(x_norm: float, y_norm: float, img_w: int, img_h: int) -> tuple[int, int]:
    """归一化坐标（0-1000）转实际像素坐标。"""
    x_norm = max(0.0, min(1000.0, float(x_norm)))
    y_norm = max(0.0, min(1000.0, float(y_norm)))
    # 将 0-1000 映射到有效像素索引，避免边界值 1000 落到窗口外。
    px = int(round(x_norm / 1000.0 * max(0, img_w - 1)))
    py = int(round(y_norm / 1000.0 * max(0, img_h - 1)))
    return px, py


def _is_cancel_requested(should_cancel: Callable[[], bool] | None) -> bool:
    """查询外部停止标记；回调异常时仅记录日志，不中断主流程。"""
    if should_cancel is None:
        return False
    try:
        return bool(should_cancel())
    except Exception as e:
        logger.warning(f"[Agent] 取消状态检查异常，忽略本次检查: {e}")
        return False


def _check_cancel(should_cancel: Callable[[], bool] | None):
    """检测到停止请求时抛出 InterruptedError，交给上层统一收尾。"""
    if _is_cancel_requested(should_cancel):
        logger.info("[Agent] 🛑 检测到停止请求，终止当前任务")
        raise InterruptedError("用户手动停止")


def _sleep_interruptible(seconds: float, should_cancel: Callable[[], bool] | None):
    """可中断睡眠，避免等待期间点击停止无响应。"""
    total = max(0.0, float(seconds))
    deadline = time.time() + total
    while True:
        _check_cancel(should_cancel)
        remain = deadline - time.time()
        if remain <= 0:
            break
        time.sleep(min(0.1, remain))


def _parse_click_coord(value: object, field_name: str) -> int:
    """将模型返回的 x/y 解析为 0-1000 的整数坐标。"""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 不能是布尔值")

    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError(f"{field_name} 不能为空")
        try:
            num = float(s)
        except Exception:
            raise ValueError(f"{field_name} 不是数字: {value}")
    else:
        raise ValueError(f"{field_name} 类型错误: {type(value).__name__}")

    if not math.isfinite(num):
        raise ValueError(f"{field_name} 不是有限数值")
    if not (0.0 <= num <= 1000.0):
        raise ValueError(f"{field_name} 超出范围: {num}，必须在 0-1000")
    return int(round(num))


def _is_time_dropdown_scroll_reason(reason: str) -> bool:
    """根据滚动原因文本判断是否处于时间下拉列表场景。"""
    text = (reason or "").lower()
    return any(k in text for k in SCROLL_REASON_TIME_KEYWORDS)


def _estimate_image_change_ratio(before_img: Image.Image, after_img: Image.Image) -> float:
    """估算两张截图的像素变化比例（0~1），用于判断滚动是否生效。"""
    if before_img.size != after_img.size:
        return 1.0

    try:
        before_rgb = before_img.convert("RGB")
        after_rgb = after_img.convert("RGB")
        diff = ImageChops.difference(before_rgb, after_rgb).convert("L")
        hist = diff.histogram()
        total = sum(hist) if hist else 0
        if total <= 0:
            return 0.0
        unchanged = hist[0]
        changed_ratio = 1.0 - (unchanged / total)
        return max(0.0, min(1.0, changed_ratio))
    except Exception as e:
        logger.warning(f"[Agent] 估算截图变化比例失败，按有变化处理: {e}")
        return 1.0


def _execute_tool(
    tool_name: str,
    args: dict,
    hwnd: int,
    dry_run: bool,
    current_img: Image.Image,
    should_cancel: Callable[[], bool] | None = None,
    runtime_state: dict[str, object] | None = None,
) -> tuple[str, Image.Image | None]:
    """
    执行 AI 选择的工具。

    Returns:
        (结果描述字符串, 新截图 Image 或 None)
        如果返回了新截图说明界面已经变化，主循环应使用该截图。
    """
    _check_cancel(should_cancel)

    if tool_name == "click":
        # 参数防御校验：x 或 y 缺失时返回错误描述，让 AI 重试，而不是 KeyError 崩溃
        if "x" not in args or "y" not in args:
            missing = [k for k in ("x", "y") if k not in args]
            logger.error(f"[Agent] ⚠️ click 工具缺少参数: {missing}，跳过本次点击")
            return f"错误：click 工具缺少必要参数 {missing}。请重新提供完整的 x、y 坐标（0-1000 整数）。", None

        try:
            x = _parse_click_coord(args.get("x"), "x")
            y = _parse_click_coord(args.get("y"), "y")
        except ValueError as e:
            logger.error(f"[Agent] ⚠️ click 坐标无效: x={args.get('x')} y={args.get('y')}，错误: {e}")
            return f"错误：{e}。请重新提供合法 x、y 坐标（0-1000 数字）。", None

        reason = args.get("reason", "")
        logger.info(f"[Agent] 🖱️ 点击 ({x}, {y})  原因：{reason}")

        # DRY_RUN 不在此处拦截！
        # 测试模式的控制完全由 System Prompt 实现：
        # AI 在识别到最终「确定」按钮时会主动调用 task_complete 而非 click。
        # 这样座位点击、时间选择等所有中间步骤都能真实执行。
        img_w, img_h = current_img.size
        px, py = _normalize_to_pixel(x, y, img_w, img_h)
        window_ctrl.post_click(hwnd, px, py)
        # 点击后固定等待 0.8 秒，确保 WebView/下拉框等 UI 元素有足够时间渲染，
        # 避免下一步截图时界面尚未响应而导致 AI 误判"点击无效"
        _sleep_interruptible(0.8, should_cancel)
        return f"已点击坐标 ({x}, {y})（像素 {px}, {py}）", None

    elif tool_name == "scroll":
        direction = str(args.get("direction", "")).lower()
        if direction not in ("up", "down"):
            logger.error(f"[Agent] ⚠️ scroll 参数非法 direction={args.get('direction')}")
            return "错误：scroll.direction 必须是 up 或 down。", None

        try:
            clicks = int(float(args.get("clicks", 3)))
        except Exception:
            logger.error(f"[Agent] ⚠️ scroll 参数非法 clicks={args.get('clicks')}")
            return "错误：scroll.clicks 必须是数字。", None

        reason = args.get("reason", "")
        if clicks <= 0:
            logger.error(f"[Agent] ⚠️ scroll.clicks 非法（需 > 0）: {clicks}")
            return "错误：scroll.clicks 必须大于 0。", None

        requested_clicks = clicks
        max_clicks_allowed = (
            SCROLL_MAX_CLICKS_TIME_DROPDOWN
            if _is_time_dropdown_scroll_reason(reason)
            else SCROLL_MAX_CLICKS_GENERAL
        )
        if clicks > max_clicks_allowed:
            logger.warning(
                f"[Agent] ⚠️ scroll.clicks 过大，已限幅 {requested_clicks} -> {max_clicks_allowed}"
                f"（reason={reason}）"
            )
            clicks = max_clicks_allowed

        logger.info(f"[Agent] 🔄 滚动 {direction} {clicks} 格  原因：{reason}")
        window_ctrl.scroll_window(hwnd, clicks=clicks, direction=direction)
        _sleep_interruptible(1.2, should_cancel)  # 等待 WebView 重绘
        _check_cancel(should_cancel)
        new_img = window_ctrl.capture_window(hwnd)
        _save_screenshot(new_img, f"scroll_{direction}")

        change_ratio = _estimate_image_change_ratio(current_img, new_img)
        no_change = change_ratio <= SCROLL_NO_CHANGE_RATIO_THRESHOLD

        no_change_streak = 1 if no_change else 0
        if runtime_state is not None:
            last_dir = str(runtime_state.get("last_scroll_direction", ""))
            streak_raw = runtime_state.get("scroll_no_change_streak", 0)
            try:
                prev_streak = int(streak_raw)
            except Exception:
                prev_streak = 0

            if no_change:
                no_change_streak = prev_streak + 1 if last_dir == direction else 1
                runtime_state["scroll_no_change_streak"] = no_change_streak
            else:
                runtime_state["scroll_no_change_streak"] = 0

            runtime_state["last_scroll_direction"] = direction

        suffix = "（已自动限幅）" if requested_clicks != clicks else ""
        if no_change:
            return (
                f"已向 {direction} 滚动 {clicks} 格{suffix}，但页面几乎无变化"
                f"（变化率 {change_ratio:.2%}，同方向连续无变化 {no_change_streak} 次），"
                "疑似已到该方向边界。"
                f"请停止继续向 {direction} 滚动；若目标编号应在反方向请改向查找；"
                "若已覆盖边界仍找不到目标，请按优先级切换下一座位，全部失败则调用 task_failed。"
            ), new_img

        return (
            f"已向 {direction} 滚动 {clicks} 格{suffix}，页面变化率 {change_ratio:.2%}，已更新截图"
        ), new_img

    elif tool_name == "wait_and_capture":
        try:
            seconds = float(args.get("seconds", 1.5))
        except Exception:
            logger.error(f"[Agent] ⚠️ wait_and_capture 参数非法 seconds={args.get('seconds')}")
            return "错误：wait_and_capture.seconds 必须是数字。", None
        if not math.isfinite(seconds):
            return "错误：wait_and_capture.seconds 不是有效数值。", None
        seconds = max(0.0, min(10.0, seconds))

        reason = args.get("reason", "")
        logger.info(f"[Agent] ⏳ 等待 {seconds}s  原因：{reason}")
        _sleep_interruptible(seconds, should_cancel)
        _check_cancel(should_cancel)
        new_img = window_ctrl.capture_window(hwnd)
        _save_screenshot(new_img, "wait_capture")
        return f"已等待 {seconds}s 并重新截图", new_img

    elif tool_name == "get_current_time":
        from datetime import timezone, timedelta
        tz_beijing = timezone(timedelta(hours=8))
        now_bj = datetime.now(tz_beijing)
        time_str = now_bj.strftime("%Y年%m月%d日 %H:%M")
        logger.info(f"[Agent] 🕐 当前北京时间: {time_str}")
        return f"当前北京时间（CST，UTC+8）：{time_str}", None

    elif tool_name in ("task_complete", "task_failed"):
        # 终止信号，由主循环处理
        return "", None

    elif tool_name == "ask_user_for_help":
        question = args.get("question", "需要您的指导")
        reason = args.get("reason", "")
        logger.info(f"[Agent] 🙋 求助用户: {question} (原因: {reason})")
        
        import tkinter as tk
        from tkinter import simpledialog
        
        def _ask():
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
            ans = simpledialog.askstring("AI 需要帮助", f"原因: {reason}\n\n问题: {question}", parent=root)
            root.destroy()
            return ans
            
        user_reply = _ask()
        if user_reply:
            logger.info(f"[Agent] 👤 用户答复: {user_reply}")
            return f"用户答复了你的问题: {user_reply}。请紧密遵循该指导继续你的任务。", None
        else:
            logger.warning("[Agent] 👤 用户取消了输入或未给出有效答复")
            return "用户未给出答复（或关闭了窗口）。请尝试重新分析当前截图或者通过你已知的规则进行重试/盲猜。", None

    return f"未知工具: {tool_name}", None


# ============================================================
# Agent 主循环
# ============================================================

# 对话历史中最多保留多少轮内含截图的消息
# 超出后将旧图片替换为文字占位符，避免 token 超载
MAX_HISTORY_IMAGES = 3


def _build_seat_targets_for_prompt(seats: list[str], max_show: int = 80) -> str:
    """构建用于提示词的座位目标字符串，避免过长占用过多 token。"""
    if not seats:
        return "未配置"
    if len(seats) <= max_show:
        return " -> ".join(seats)
    return f"{' -> '.join(seats[:max_show])} -> ... -> {seats[-1]}（共{len(seats)}个）"


def _build_seat_targets_for_log(seats: list[str], max_show: int = 20) -> str:
    """构建用于日志输出的简短座位摘要。"""
    if not seats:
        return "未配置"
    if len(seats) <= max_show:
        return " -> ".join(seats)
    return f"{' -> '.join(seats[:max_show])} -> ...（共{len(seats)}个）"


def run_agent(
    hwnd: int,
    dry_run: bool = False,
    start_time: str | None = None,
    end_time: str | None = None,
    max_steps: int = 50,
    should_cancel: Callable[[], bool] | None = None,
    target_window_title: str | None = None,
    target_window_class_name: str | None = None,
    target_window_pid: int | None = None,
) -> bool:
    """
    启动 Agent 主循环，让 AI 自主完成预约全流程。

    Args:
        hwnd:       已锁定的目标窗口句柄
        dry_run:    测试模式（True 时不点击最终「确定」按钮）
        start_time: 预约开始时间（如 "8:00"），None 则读 config
        end_time:   预约结束时间（如 "22:00"），None 则读 config
        max_steps:  最大循环步数（安全上限，防止无限循环）
        should_cancel: 外部停止检查函数，返回 True 时中断任务
        target_window_title: 目标窗口标题，用于句柄恢复
        target_window_class_name: 目标窗口类名，用于句柄恢复
        target_window_pid: 目标窗口 PID，用于句柄恢复

    Returns:
        bool: 是否成功完成预约
    """
    _start = start_time or config.BOOKING_START_TIME
    _end   = end_time   or config.BOOKING_END_TIME

    target_seats = config.get_target_seats() if hasattr(config, "get_target_seats") else list(config.PREFERRED_SEATS)
    if not target_seats:
        raise BookingError("未配置有效目标座位，请先在界面里保存具体座位或座位段。")

    seat1 = target_seats[0]
    seat2 = target_seats[1] if len(target_seats) > 1 else "无"
    seat_targets = _build_seat_targets_for_prompt(target_seats)

    seat_mode = getattr(config, "SEAT_MODE", "list")
    if seat_mode == "range":
        range_start = getattr(config, "SEAT_RANGE_START", "")
        range_end = getattr(config, "SEAT_RANGE_END", "")
        if range_start and range_end:
            seat_mode_desc = f"连续座位段（{range_start}-{range_end}）"
        else:
            seat_mode_desc = "连续座位段"
    else:
        seat_mode_desc = "具体座位优先级列表"

    # ---- 加载提示词（每次运行时实时读取，保证 GUI 修改立刻生效）----
    try:
        raw_prompt = load_prompt()
    except Exception as e:
        raise BookingError(str(e))
    try:
        system_prompt = raw_prompt.format(
            seat1=seat1, seat2=seat2,
            seat_targets=seat_targets,
            seat_mode_desc=seat_mode_desc,
            start_time=_start, end_time=_end,
        )
    except KeyError as e:
        logger.warning(f"提示词中存在无法识别的占位符 {e}，将原样使用提示词")
        system_prompt = raw_prompt

    # DRY_RUN 附加说明（追加到 System Prompt 末尾）
    if dry_run:
        system_prompt += (
            "\n\n⚠️ **当前为测试（DRY_RUN）模式**，规则如下：\n"
            "- **步骤1~7 全部真实执行**：先点右上角三个点并重新进入小程序，再点「预约选座」Tab、"
            "点击「筛选」按钮、设置开始/结束时间、点击筛选弹窗的「确定」、点击座位——全部照常调用 click 工具真实操作。\n"
            "- **【!!!FORBIDDEN!!!】步骤8 绝对禁止点击**：当你定位到最终蓝色「确定」按钮后，"
            "**不要调用 click**，在测试状态下,极有可能导致用户操作违规!!!改为调用 task_complete即可"
            "在 message 中说明已识别到「确定」按钮的坐标但未提交，测试流程结束。"
        )

    # ---- 初始化对话 ----
    client = OpenAI(api_key=config.API_KEY, base_url=config.API_BASE_URL)
    messages = [{"role": "system", "content": system_prompt}]

    logger.info("=" * 55)
    logger.info(f"[Agent] 启动 AI 自动预约（{'DRY_RUN 测试' if dry_run else '正式执行'}）")
    logger.info(f"[Agent] 目标座位: {_build_seat_targets_for_log(target_seats)}")
    logger.info(f"[Agent] 预约时间: {_start} ~ {_end}")
    logger.info(f"[Agent] 最大步数: {max_steps}，历史图片保留: {MAX_HISTORY_IMAGES}")
    logger.info("=" * 55)

    current_hwnd = hwnd
    last_window_title = (target_window_title or "").strip() or window_ctrl.get_window_title(current_hwnd)
    last_window_class_name = (target_window_class_name or "").strip() or window_ctrl.get_window_class_name(current_hwnd)
    last_window_pid = target_window_pid if target_window_pid is not None else window_ctrl.get_window_process_id(current_hwnd)

    def _refresh_window_signature() -> None:
        """在句柄仍有效时刷新窗口签名，提升后续重获句柄的准确率。"""
        nonlocal last_window_title, last_window_class_name, last_window_pid

        if not window_ctrl.is_window_handle_valid(current_hwnd):
            return

        title_now = window_ctrl.get_window_title(current_hwnd)
        class_now = window_ctrl.get_window_class_name(current_hwnd)
        pid_now = window_ctrl.get_window_process_id(current_hwnd)

        if title_now:
            last_window_title = title_now
        if class_now:
            last_window_class_name = class_now
        if pid_now is not None:
            last_window_pid = pid_now

    def _recover_window_handle() -> int | None:
        """按窗口签名恢复句柄；失败时回退到标题匹配。"""
        recovered = window_ctrl.find_window_for_recovery(
            last_window_title,
            last_window_class_name,
            last_window_pid,
        )
        if recovered is not None:
            return recovered

        if last_window_title:
            return window_ctrl.find_window_by_title(last_window_title)

        return None

    def _is_invalid_hwnd_error(err: Exception) -> bool:
        text = str(err)
        low = text.lower()
        return (
            "无效的窗口句柄" in text
            or "invalid window handle" in low
            or ("1400" in text and "window" in low)
        )

    def _ensure_hwnd_available() -> bool:
        nonlocal current_hwnd, last_window_title, last_window_class_name, last_window_pid

        if window_ctrl.is_window_handle_valid(current_hwnd):
            _refresh_window_signature()
            return True

        logger.warning(
            f"[Agent] ⚠️ 检测到窗口句柄失效: {current_hwnd}。"
            f"3 秒后按窗口签名恢复（标题: {last_window_title or '未知'}，"
            f"类名: {last_window_class_name or '未知'}，PID: {last_window_pid or '未知'}）"
        )
        _sleep_interruptible(3.0, should_cancel)
        _check_cancel(should_cancel)

        if not last_window_title and not last_window_class_name and last_window_pid is None:
            logger.error("[Agent] ❌ 无法恢复窗口句柄：缺少历史窗口签名")
            return False

        recovered = _recover_window_handle()
        if recovered is None:
            logger.error(
                f"[Agent] ❌ 未找到可恢复窗口（标题: {last_window_title or '未知'}，"
                f"类名: {last_window_class_name or '未知'}，PID: {last_window_pid or '未知'}），无法继续"
            )
            return False

        old_hwnd = current_hwnd
        current_hwnd = recovered
        _refresh_window_signature()
        logger.info(f"[Agent] ✅ 句柄恢复成功: {old_hwnd} -> {current_hwnd}")
        return True

    # 记录哪些 messages 下标包含图片（用于滑动清理）
    image_msg_indices: list[int] = []
    runtime_state: dict[str, object] = {
        "last_scroll_direction": "",
        "scroll_no_change_streak": 0,
    }

    for step in range(1, max_steps + 1):
        _check_cancel(should_cancel)
        logger.info(f"\n[Agent] ══ Step {step}/{max_steps} ══")

        # ---- 截图 ----
        if not _ensure_hwnd_available():
            return False

        try:
            img = window_ctrl.capture_window(current_hwnd)
        except Exception as e:
            if _is_invalid_hwnd_error(e):
                logger.warning("[Agent] 截图时句柄失效，执行 3 秒等待 + 同标题窗口恢复")
                if not _ensure_hwnd_available():
                    return False
                try:
                    img = window_ctrl.capture_window(current_hwnd)
                except Exception as e2:
                    logger.error(f"[Agent] 句柄恢复后截图仍失败: {e2}")
                    return False
            else:
                logger.error(f"[Agent] 截图失败: {e}")
                return False
        _save_screenshot(img, f"step{step:02d}")

        # ---- 历史图片滑动窗口清理 ----
        if len(image_msg_indices) >= MAX_HISTORY_IMAGES:
            oldest_idx = image_msg_indices.pop(0)
            old_msg = messages[oldest_idx]
            if isinstance(old_msg.get("content"), list):
                # 保留文字部分，移除图片内容
                text_parts = [p for p in old_msg["content"] if p.get("type") == "text"]
                messages[oldest_idx]["content"] = text_parts + [
                    {"type": "text", "text": "[旧截图已自动清除，节省 Token]"}
                ]

        # ---- 构建本步 user 消息（含截图）----
        img_b64 = _image_to_base64(img)
        user_msg = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                },
                {
                    "type": "text",
                    "text": (
                        f"这是第 {step} 步的当前界面截图。"
                        "请先判断当前处于哪个界面，然后调用相应工具完成下一步操作。"
                    ),
                },
            ],
        }
        messages.append(user_msg)
        image_msg_indices.append(len(messages) - 1)

        # ---- 调用 AI ----
        _check_cancel(should_cancel)
        try:
            response = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                extra_body={"enable_thinking": config.AGENT_ENABLE_THINKING},
                max_tokens=2048,
            )
        except Exception:
            logger.exception("[Agent] API 调用失败")
            return False

        _check_cancel(should_cancel)

        ai_message = response.choices[0].message

        # 打印思考过程（如果开启了思考模式）
        if hasattr(ai_message, "reasoning_content") and ai_message.reasoning_content:
            logger.info(f"[Agent] 思考: {ai_message.reasoning_content[:400]}...")

        # ---- 解析 tool_call ----
        if not ai_message.tool_calls:
            # AI 返回了文字而非工具调用（可能在思考或解释）
            text_content = ai_message.content or ""
            logger.warning(f"[Agent] ⚠️ AI 未调用工具，回复: {text_content[:200]}")
            messages.append({"role": "assistant", "content": text_content})
            # 追加一条指引消息，让 AI 下一轮必须调用工具
            messages.append({
                "role": "user",
                "content": "请根据上方截图直接调用工具，不要仅用文字回复。",
            })
            continue

        tool_call = ai_message.tool_calls[0]
        tool_name = tool_call.function.name

        raw_tool_args = tool_call.function.arguments
        try:
            if not isinstance(raw_tool_args, str):
                raise TypeError(f"arguments 类型非法: {type(raw_tool_args).__name__}")
            tool_args = json.loads(raw_tool_args)
        except Exception as parse_err:
            # JSON 格式损坏（如坐标分行、缺少键名等），不执行工具，反馈给 AI 让它重试
            bad_raw = str(raw_tool_args)
            logger.error(f"[Agent] ⚠️ 工具参数 JSON 解析失败，跳过本次调用并要求重试")
            logger.error(f"[Agent]    原始内容: {bad_raw}")
            logger.error(f"[Agent]    错误详情: {parse_err}")
            messages.append(ai_message)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": (
                    f"参数格式错误，无法解析（原始: {bad_raw[:120]}）。"
                    "请重新生成工具调用，确保 JSON 格式正确、x 和 y 参数均为整数且在同一行。"
                ),
            })
            continue

        if not isinstance(tool_args, dict):
            logger.error(f"[Agent] ⚠️ 工具参数类型错误: {type(tool_args).__name__}")
            messages.append(ai_message)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": "参数格式错误：工具参数必须是 JSON 对象。请按工具 schema 重新生成参数。",
            })
            continue

        logger.info(f"[Agent] 🔧 工具: {tool_name}  参数: {json.dumps(tool_args, ensure_ascii=False)}")

        # ---- 处理终止工具 ----
        if tool_name == "task_complete":
            msg = tool_args.get("message", "")
            logger.info(f"[Agent] ✅ 任务完成: {msg}")
            return True

        if tool_name == "task_failed":
            reason = tool_args.get("reason", "")
            logger.error(f"[Agent] ❌ 任务失败: {reason}")
            raise BookingError(reason)

        # ---- 执行工具 ----
        if not _ensure_hwnd_available():
            return False

        try:
            result_str, new_img = _execute_tool(
                tool_name,
                tool_args,
                current_hwnd,
                dry_run,
                img,
                should_cancel=should_cancel,
                runtime_state=runtime_state,
            )
        except InterruptedError:
            raise
        except Exception as tool_err:
            if _is_invalid_hwnd_error(tool_err):
                logger.warning(f"[Agent] 工具执行时句柄失效（{tool_name}），尝试恢复后重试一次")
                if _ensure_hwnd_available():
                    try:
                        result_str, new_img = _execute_tool(
                            tool_name,
                            tool_args,
                            current_hwnd,
                            dry_run,
                            img,
                            should_cancel=should_cancel,
                            runtime_state=runtime_state,
                        )
                        logger.info(f"[Agent] 工具重试成功: {tool_name}")
                    except InterruptedError:
                        raise
                    except Exception as retry_err:
                        logger.exception(f"[Agent] ⚠️ 工具重试后仍失败: {tool_name}")
                        result_str = (
                            "工具执行失败："
                            f"{type(retry_err).__name__}: {retry_err}。"
                            "请根据当前截图重新判断并重试；"
                            "如为坐标问题，请提供合法 x/y（0-1000 数字）。"
                        )
                        new_img = None
                else:
                    return False
            else:
                logger.exception(f"[Agent] ⚠️ 工具执行异常: {tool_name}")
                result_str = (
                    "工具执行失败："
                    f"{type(tool_err).__name__}: {tool_err}。"
                    "请根据当前截图重新判断并重试；"
                    "如为坐标问题，请提供合法 x/y（0-1000 数字）。"
                )
                new_img = None

        # 将本轮 AI 决策和工具结果加入对话历史
        messages.append(ai_message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result_str,
        })

        logger.info(f"[Agent] 工具结果: {result_str}")

        # 如果工具返回了新截图（scroll/wait_and_capture），下一轮直接使用
        # （无需再次走到循环顶部的截图逻辑）
        # 这里什么都不用做，下一轮 for 会重新截图

    logger.error(f"[Agent] 已到达最大步数 {max_steps}，强制退出")
    return False


# ============================================================
# 向外暴露的异常类（兼容 main.py 的 except BookingError 捕获）
# ============================================================

class BookingError(Exception):
    """业务级错误（座位全满、时间不存在等），由 AI 报告后抛出。"""
    pass
