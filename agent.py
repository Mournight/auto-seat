"""
智能 Agent 模块 - 用「感知-推理-行动」循环替代硬编码步骤。

核心流程：
  截图 → AI 判断当前界面状态 → AI 选择工具（click/scroll/wait）→ 执行 → 再截图 → ...

提示词（System Prompt）从 system_prompt.txt 文件加载，
不存在时退回到内置默认值。GUI 中保存后立刻对下次运行生效。
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
from PIL import Image
from openai import OpenAI

import config
import window_ctrl

logger = logging.getLogger(__name__)

# ============================================================
# 提示词模板与持久化
# ============================================================

# 模板中保留运行时占位符：
# {seat1} {seat2} {seat_targets} {seat_mode_desc} {start_time} {end_time}
# 其余花括号（如 JSON 示例）需写成 {{ }}
DEFAULT_SYSTEM_PROMPT = """\
你是一个专门操作「学生自习室预约小程序」的界面自动化 Agent。

# 你的任务
预约目标座位（**{seat_mode_desc}**）：**{seat_targets}**，时间段：**{start_time} ~ {end_time}**。

# 这个微信小程序的完整预约步骤（请严格按此顺序執行）
1. 首页底部导航栏有「预约选座」Tab → 点击它进入预约页
2. 确认处于「5号楼」Tab（若不在，先点击「5号楼」Tab）→ 点击「5号楼智能自习室」右侧蓝色「选座」按钮
3. 进入座位图（绿色格子=可用，红色=已占）→ 找到目标座位编号并点击
4. 弹出预约确认弹窗 → 点击「开始时间」下拉框
5. 在下拉列表中选择 {start_time}
6. 回到弹窗 → 点击「结束时间」下拉框
7. 在下拉列表中选择 {end_time}
8. 点击右侧蓝色「确定」按钮 → 预约完成，调用 task_complete

# 当前界面判断规则（每轮截图后必须先判断）
- 看到底部有多个图标Tab、页面内容较稀疏 → **首页**，需找并点击「预约选座」Tab
- 看到「4号楼」「5号楼」等楼层Tab + 自习室列表 → **预约选座页**
- 看到大量小格子组成的座位分布图 → **座位图页面**
- 看到含「开始时间」「结束时间」文字的弹窗界面 → **预约确认弹窗**
- 看到竖排密集排列的时间选项（如 8:00、9:00 …） → **时间下拉列表**

# 关键操作规则
1. **座位必须二次验证**：点击座位后，调用 wait_and_capture 等待弹窗出现，然后检查弹窗背景中放大的座位编号。若编号不符，点击弹窗里的「取消」，记下错误编号，再继续找正确座位。
2. **滚动查找座位**：当前截图中看不到目标座位时，根据可见编号判断目标在上方还是下方，使用 scroll 工具翻动。
3. **禁止编造坐标**：坐标必须来自对当前截图的真实观察，使用 0-1000 归一化范围，即截图左上角为(0,0)，右下角为(1000,1000)。
4. **界面变化后需截图确认**：点击或滚动后，用 wait_and_capture 等待界面响应，下一轮再做判断。
5. **判断时间栏是否已选中（重要）**：
   - 开始时间栏：只要显示的**不是灰色的「请选择开始时间」**，就说明已选定（无论显示的是"现在"、"8:00"还是任何其他时间文字），确定选择正确后，可以进行下一步，选定结束时间。
   - 结束时间栏：只要显示的**不是灰色的「请选择结束时间」**，就说明已选定，确定选择正确后，可以进行下一步，点击「确定」按钮。
   - 「现在」是合法的已选时间状态，代表系统将当前时刻作为开始时间，请接受并继续。
6. **时间下拉框操作规范（防止反复失败）**：
   - 点击「开始时间」或「结束时间」区域后，**必须立刻调用 wait_and_capture(1.5)**，确认下拉列表已展开（看到竖排多个时间选项），再去点击目标时间。
   - 下拉列表是浮层，点击列表**外部任何区域**都会让它立刻关闭。点击时间选项时要**严格对准列表内文字行的中央**，不要点到边缘外侧。
   - 若目标时间（如 22:00）不在列表可视范围内，用 scroll 工具向下滚动直到看到它，再点击。
   - 若截图显示列表已消失（回到「请选择...」状态），说明误点了外部，冷静地重新点击时间栏打开即可。

# 完成或失败条件
- **成功**：完成步骤8（点击确定）后調用 task_complete。
- **失败**：目标座位全部被占（无绿色可用）、目标时间段不在列表中、超过5次重复失败，调用 task_failed 并说明原因。
"""

# system_prompt.txt 存储路径（与代码同目录）
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")


def load_prompt() -> str:
    """加载提示词，优先从文件读取，文件不存在或为空时使用内置默认值。"""
    if os.path.exists(PROMPT_FILE):
        try:
            with open(PROMPT_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                logger.info("已从 system_prompt.txt 加载自定义提示词")
                return content
        except Exception as e:
            logger.warning(f"读取提示词文件失败: {e}，使用内置默认提示词")
    return DEFAULT_SYSTEM_PROMPT


def save_prompt(text: str) -> bool:
    """将提示词保存到 system_prompt.txt。"""
    try:
        with open(PROMPT_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info("提示词已保存到 system_prompt.txt，下次运行立刻生效")
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
                        "description": "滚动格数（建议 5-15），默认 10",
                        "default": 10,
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


def _execute_tool(
    tool_name: str,
    args: dict,
    hwnd: int,
    dry_run: bool,
    current_img: Image.Image,
    should_cancel: Callable[[], bool] | None = None,
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
            clicks = int(float(args.get("clicks", 10)))
        except Exception:
            logger.error(f"[Agent] ⚠️ scroll 参数非法 clicks={args.get('clicks')}")
            return "错误：scroll.clicks 必须是数字。", None

        if clicks <= 0 or clicks > 30:
            logger.error(f"[Agent] ⚠️ scroll.clicks 超出允许范围: {clicks}")
            return "错误：scroll.clicks 必须在 1-30 之间。", None

        reason = args.get("reason", "")
        logger.info(f"[Agent] 🔄 滚动 {direction} {clicks} 格  原因：{reason}")
        window_ctrl.scroll_window(hwnd, clicks=clicks, direction=direction)
        _sleep_interruptible(1.2, should_cancel)  # 等待 WebView 重绘
        _check_cancel(should_cancel)
        new_img = window_ctrl.capture_window(hwnd)
        _save_screenshot(new_img, f"scroll_{direction}")
        return f"已向 {direction} 滚动 {clicks} 格，已更新截图", new_img

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
    max_steps: int = 30,
    should_cancel: Callable[[], bool] | None = None,
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
    raw_prompt = load_prompt()
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
            "- **步骤1~7 全部真实执行**：点击「预约选座」Tab、点击「选座」按钮、"
            "点击座位、选择开始时间、选择结束时间——全部照常调用 click 工具真实操作。\n"
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

    # 记录哪些 messages 下标包含图片（用于滑动清理）
    image_msg_indices: list[int] = []

    for step in range(1, max_steps + 1):
        _check_cancel(should_cancel)
        logger.info(f"\n[Agent] ══ Step {step}/{max_steps} ══")

        # ---- 截图 ----
        try:
            img = window_ctrl.capture_window(hwnd)
        except Exception as e:
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
        try:
            result_str, new_img = _execute_tool(
                tool_name,
                tool_args,
                hwnd,
                dry_run,
                img,
                should_cancel=should_cancel,
            )
        except InterruptedError:
            raise
        except Exception as tool_err:
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
