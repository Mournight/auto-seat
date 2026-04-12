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

# ==================== 座位偏好配置 ====================
# 优先座位列表，从左到右优先级递减
PREFERRED_SEATS = ["120", "121"]

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
