"""
视觉 AI 模块 - 负责：
1. 将截图发送给 Qwen3.5-Plus 多模态大模型
2. 解析返回的坐标（归一化 0~1000 范围内的包围盒）
3. 将归一化坐标换算回窗口客户区像素坐标
"""
import base64
import re
import io
import logging
from PIL import Image
from openai import OpenAI
import config

logger = logging.getLogger(__name__)

# 初始化 OpenAI 兼容客户端（连接阿里云 DashScope）
_client = OpenAI(
    api_key=config.API_KEY,
    base_url=config.API_BASE_URL,
)


def _image_to_base64(img: Image.Image) -> str:
    """将 PIL Image 转为 base64 字符串（JPEG 格式，节省 Token）"""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _parse_bbox(text: str) -> tuple[float, float] | None:
    """
    从模型返回文本中解析坐标。
    支持格式：
    1. [[x1, y1, x2, y2]] (JSON 数组)
    2. <|box_start|>(x1,y1),(x2,y2)<|box_end|>
    3. <box>(y1,x1),(y2,x2)</box>
    4. (x, y) 简单坐标对
    """
    # 1. 优先匹配 JSON 数组格式 [[x1, y1, x2, y2]]
    json_match = re.search(r'\[\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]\]', text)
    if json_match:
        x1, y1, x2, y2 = map(int, json_match.groups())
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    # 2. 匹配 Qwen3 新格式：<|box_start|>(x1,y1),(x2,y2)<|box_end|>
    pattern_new = r'<\|box_start\|>\((\d+),(\d+)\),\((\d+),(\d+)\)<\|box_end\|>'
    m = re.search(pattern_new, text)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    # 3. 兼容旧格式：<box>(y1,x1),(y2,x2)</box>
    pattern_old = r'<box>\((\d+),(\d+)\),\((\d+),(\d+)\)</box>'
    m = re.search(pattern_old, text)
    if m:
        y1, x1, y2, x2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    # 4. 兜底匹配：直接找 (x, y) 形式的数值对
    pattern_simple = r'\((\d{1,3}),\s*(\d{1,3})\)'
    matches = re.findall(pattern_simple, text)
    if matches:
        cx, cy = float(matches[0][0]), float(matches[0][1])
        if cx <= 100 and cy <= 100:
            return cx * 10, cy * 10
        return cx, cy

    return None


def normalize_to_pixel(cx_norm: float, cy_norm: float, img_w: int, img_h: int) -> tuple[int, int]:
    """
    将归一化坐标（0~999）转换为实际像素坐标。

    Args:
        cx_norm: 归一化 x（0~999）
        cy_norm: 归一化 y（0~999）
        img_w: 图像实际宽度（像素）
        img_h: 图像实际高度（像素）

    Returns:
        (px, py): 实际像素坐标
    """
    px = int(cx_norm / 999.0 * img_w)
    py = int(cy_norm / 999.0 * img_h)
    return px, py


def ask_model(img: Image.Image, prompt: str) -> tuple[str, tuple[int, int] | None]:
    """
    将截图和 prompt 发送给 Qwen3.5-Plus，返回模型原始回复文本和解析到的像素坐标。

    Args:
        img: 截图（PIL Image）
        prompt: 向模型提出的问题/指令

    Returns:
        (raw_text, pixel_coord):
            raw_text: 模型原始回复
            pixel_coord: 解析到的像素坐标 (px, py)，解析失败则为 None
    """
    img_b64 = _image_to_base64(img)
    img_w, img_h = img.size

    logger.info(f"向 Qwen 发送截图（{img_w}x{img_h}），提示: {prompt[:80]}...")

    response = _client.chat.completions.create(
        model=config.MODEL_NAME,
        # 关闭思考模式，减少延时
        extra_body={"enable_thinking": False},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ],
        max_tokens=512,
    )

    raw_text = response.choices[0].message.content or ""
    logger.info(f"Qwen 回复: {raw_text[:200]}")

    # 解析坐标
    coord_norm = _parse_bbox(raw_text)
    if coord_norm is not None:
        px, py = normalize_to_pixel(coord_norm[0], coord_norm[1], img_w, img_h)
        logger.info(f"解析坐标: 归一化({coord_norm[0]:.1f}, {coord_norm[1]:.1f}) -> 像素({px}, {py})")
        return raw_text, (px, py)
    else:
        logger.warning(f"未能从回复中解析出坐标: {raw_text[:200]}")
        return raw_text, None
