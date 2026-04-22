"""
全局配置文件 - 自习室自动预约机器人
"""

import os
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

# ==================== 阿里云 DashScope 配置 ====================
API_KEY = os.getenv("API_KEY")
API_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# qwen3.5-plus 是 Qwen3.5 系列商业版，原生支持图像输入（2026 年 2 月发布）
MODEL_NAME = "qwen3.5-plus"

# 是否开启 AI 思考模式（enable_thinking）
# True  = 模型在选择工具前先推理，决策质量更高，但每步约多 3-10 秒
# False = 直接输出工具调用，速度快，适合网络好/情况简单的预约场景
AGENT_ENABLE_THINKING = True

# Agent 最大步数（安全上限，防止流程异常时无限循环）
# 可在 GUI 中设置并保存到 .env
try:
    AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "50"))
except Exception:
    AGENT_MAX_STEPS = 50

if AGENT_MAX_STEPS < 1:
    AGENT_MAX_STEPS = 1
elif AGENT_MAX_STEPS > 300:
    AGENT_MAX_STEPS = 300

# ==================== 座位偏好配置 ====================
def _parse_seat_list(raw: str | None) -> list[str]:
    """解析座位字符串（逗号/空格分隔），并去重保序。"""
    if not raw:
        return []
    normalized = raw.replace("，", ",").replace("、", ",").replace(" ", ",")
    seats = [s.strip() for s in normalized.split(",") if s.strip()]
    uniq: list[str] = []
    seen: set[str] = set()
    for s in seats:
        if s not in seen:
            uniq.append(s)
            seen.add(s)
    return uniq


# 座位策略：
# - list  = 按优先级使用具体座位列表（PREFERRED_SEATS）
# - range = 使用连续座位段（SEAT_RANGE_START ~ SEAT_RANGE_END）
SEAT_MODE = os.getenv("SEAT_MODE", "list").strip().lower()
if SEAT_MODE not in ("list", "range"):
    SEAT_MODE = "list"

_env_preferred = _parse_seat_list(os.getenv("PREFERRED_SEATS"))
PREFERRED_SEATS = _env_preferred if _env_preferred else ["120", "121"]

SEAT_RANGE_START = os.getenv("SEAT_RANGE_START", "").strip()
SEAT_RANGE_END = os.getenv("SEAT_RANGE_END", "").strip()

# 连续座位段最大长度，防止一次注入过长目标列表导致提示词膨胀
MAX_SEAT_RANGE_SPAN = 300


def get_target_seats() -> list[str]:
    """根据当前座位策略，返回最终目标座位列表。"""
    if SEAT_MODE == "range":
        if SEAT_RANGE_START.isdigit() and SEAT_RANGE_END.isdigit():
            start = int(SEAT_RANGE_START)
            end = int(SEAT_RANGE_END)
            if start > end:
                start, end = end, start
            end = min(end, start + MAX_SEAT_RANGE_SPAN - 1)
            return [str(x) for x in range(start, end + 1)]
        # 座位段配置非法时回退到列表，避免运行期直接空配置
        return PREFERRED_SEATS
    return PREFERRED_SEATS


def get_seat_display_text() -> str:
    """生成用于 UI 显示的座位策略摘要。"""
    if SEAT_MODE == "range":
        if SEAT_RANGE_START and SEAT_RANGE_END:
            return f"座位段: {SEAT_RANGE_START}-{SEAT_RANGE_END}"
        return "座位段: 未完整配置"
    if not PREFERRED_SEATS:
        return "座位列表: 未配置"
    return f"座位列表: {' -> '.join(PREFERRED_SEATS)}"

# ==================== 时间配置 ====================
# 预约开始时间（小时:分钟）- 每天在此时间点触发
TRIGGER_TIME = os.getenv("TRIGGER_TIME", "06:00")
try:
    _h, _m = TRIGGER_TIME.split(":")
    TRIGGER_HOUR = int(_h)
    TRIGGER_MINUTE = int(_m)
except Exception:
    TRIGGER_HOUR = 6
    TRIGGER_MINUTE = 0

# 预约的时间段（开始时间 -> 结束时间）
# 根据你截图中看到的可选时间段，设置你需要预约的时间
BOOKING_START_TIME = os.getenv("BOOKING_START_TIME", "08:00")
BOOKING_END_TIME = os.getenv("BOOKING_END_TIME", "22:00")



# ==================== 重试配置 ====================
MAX_RETRY_TIMES = 3           # 每步操作最大重试次数
RETRY_INTERVAL_SEC = 2.0      # 重试间隔（秒）
STEP_WAIT_SEC = 1.5           # 每步操作后的等待时间（秒）

# ==================== 调试配置 ====================
# 设为 True 时，最后一步「确定」按钮不会真正点击（干跑模式，用于测试）
DRY_RUN = True
