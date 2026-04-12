"""
预约状态机 - 负责：
按照截图 -> 识别 -> 点击 的闭环，依次完成以下 5 个步骤：

步骤 1: 点击底部「预约选座」Tab
步骤 2: 选择「5号楼」并点击「选座」按钮
步骤 3: 在座位图中点击目标座位（优先 120，否则 121）
步骤 4: 点击「开始时间」选择框
步骤 5: 选择开始时间，然后选择结束时间，最后点击「确定」
"""
import time
import logging
import os
from datetime import datetime
from PIL import Image

import config
import window_ctrl
import vision_ai

logger = logging.getLogger(__name__)

class BookingError(Exception):
    """业务逻辑错误（例如座位被抢空、时间选项不存在等）"""
    pass

# ============================================================
# 每步操作的 Prompt 模板
# ============================================================

PROMPT_STEP1_BOOKING_TAB = (
    "这是一个手机缩放后的微信自习室预约小程序截图。"
    "请在截图中找到底部导航栏的「预约选座」按钮，"
    "用 <box>[[x1,y1,x2,y2]]</box> 格式返回它的边界框坐标（0-1000 比例）。"
)

PROMPT_STEP2_SELECT_SEAT_BTN = (
    "这是自习室预约小程序的「预约选座」页面截图。"
    "页面顶部有楼层 Tab（4号楼、5号楼），下方是时间选择区域和自习室列表。"
    "请找到列表中「5号楼智能自习室」右侧的蓝色「选座」按钮，"
    "用 <box>[[x1,y1,x2,y2]]</box> 格式返回坐标（0-1000 比例）。"
)

PROMPT_STEP3_TEMPLATE = (
    "这是自习室座位分布图截图，座位图中绿色底座代表当前可用，红色人物代表已被预约。\n"
    "请在当前可见区域内，优先找编号为「{target1}」的绿色可用座位；"
    "若该座位被占用（红色），则找「{target2}」号座位。\n"
    "{avoid_str}\n"
    "注意：只能点击绿色（可用）座位。\n"
    "如果目标座位在当前截图中没有任何一处可见（全部超出屏幕范围），请不要编造坐标！\n"
    "此时请观察当前图片中能看到的边缘座位编号，判断目标座位的大致方位：\n"
    "- 如果目标座位应该在当前这批编号的【更上方】，请只回复 SCROLL_UP\n"
    "- 如果目标座位应该在当前这批编号的【更下方】，请只回复 SCROLL_DOWN\n"
    "如果找到了目标座位，用 <box>[[x1,y1,x2,y2]]</box> 格式返回坐标（0-1000 比例）。"
)

PROMPT_STEP4_START_TIME = (
    "这是座位预约的弹窗截图，弹窗中有「开始时间」和「结束时间」两行表单。"
    "请找到「开始时间」那一行右侧显示「请选择开始时间」的可点击区域，"
    "用 <box>[[x1,y1,x2,y2]]</box> 格式返回坐标（0-1000 比例）。"
)

# 步骤5、7的Prompt在运行时动态构建（见 run_booking），以便 GUI 中的时间设置能实时生效
PROMPT_STEP5_TEMPLATE = (
    "这是一个时间选择下拉列表，列出了多个可选时间段。"
    "请找到选项「{start_time}」所在的行，"
    "如果图片中根本没有出现「{start_time}」这个选项，请只回复 ERROR_NOT_FOUND，不要编造坐标。"
    "如果找到了，用 <box>[[x1,y1,x2,y2]]</box> 格式返回它的坐标（0-1000 比例）。"
)

PROMPT_STEP6_END_TIME_INPUT = (
    "这是座位预约的弹窗截图，弹窗中有「开始时间」和「结束时间」两行表单。"
    "请找到「结束时间」那一行右侧显示「请选择结束时间」或已选时间的可点击区域，"
    "用 <box>[[x1,y1,x2,y2]]</box> 格式返回坐标（0-1000 比例）。"
)

PROMPT_STEP7_TEMPLATE = (
    "这是一个时间选择下拉列表，列出了多个可选时间段。"
    "请找到选项「{end_time}」所在的行，"
    "如果图片中根本没有出现「{end_time}」这个选项，请只回复 ERROR_NOT_FOUND，不要编造坐标。"
    "如果找到了，用 <box>[[x1,y1,x2,y2]]</box> 格式返回它的坐标（0-1000 比例）。"
)

PROMPT_STEP8_CONFIRM = (
    "这是座位预约的确认弹窗，底部有「取消」和蓝色「确定」两个按钮。"
    "请找到右侧蓝色的「确定」按钮，"
    "用 <box>[[x1,y1,x2,y2]]</box> 格式返回坐标（0-1000 比例）。"
)


# ============================================================
# 核心：带重试的单步执行
# ============================================================

def _save_screenshot(img: Image.Image, step_name: str):
    """将截图保存到 screenshots 目录，用于调试"""
    os.makedirs("screenshots", exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    path = os.path.join("screenshots", f"{ts}_{step_name}.jpg")
    img.save(path, "JPEG", quality=85)
    logger.info(f"截图已保存: {path}")


def _execute_step(
    hwnd: int,
    step_name: str,
    prompt: str,
    dry_run: bool = False,
    wait_after: float = config.STEP_WAIT_SEC,
) -> bool:
    """
    执行「截图 -> AI识别 -> 后台点击」的完整一步，自动重试。

    Args:
        hwnd: 目标窗口句柄
        step_name: 步骤名称（日志用）
        prompt: 传给 AI 的提示词
        dry_run: 如果为 True，识别到坐标后不真正点击
        wait_after: 点击后等待秒数

    Returns:
        bool: 是否成功
    """
    for attempt in range(1, config.MAX_RETRY_TIMES + 1):
        try:
            logger.info(f"[{step_name}] 第 {attempt} 次尝试...")

            # 截图
            img = window_ctrl.capture_window(hwnd)
            _save_screenshot(img, step_name)
            img_w, img_h = img.size

            # AI 识别
            raw_text, coord = vision_ai.ask_model(img, prompt)

            # 业务层未找到时的友好错误判定
            if "ERROR_NOT_FOUND" in (raw_text or "").upper():
                logger.error(f"[{step_name}] 报告该项在画面中不存在。")
                raise BookingError(f"[{step_name}] 失败：界面上找不到您选择的这个目标。可能是时间已过，或该座位已被抢完。")

            if coord is None:
                logger.warning(f"[{step_name}] AI 未返回有效坐标，{config.RETRY_INTERVAL_SEC}s 后重试...")
                logger.debug(f"AI 原始回复: {raw_text}")
                time.sleep(config.RETRY_INTERVAL_SEC)
                continue

            px, py = coord
            logger.info(f"[{step_name}] 目标坐标: ({px}, {py})")

            if dry_run:
                logger.info(f"[{step_name}] 🔵 DRY_RUN 模式，跳过实际点击。坐标=({px}, {py})")
                print(f"  ✅ [{step_name}] 识别成功 -> 坐标 ({px}, {py})  [DRY_RUN，未点击]")
                return True

            # 后台点击
            window_ctrl.post_click(hwnd, px, py)
            print(f"  ✅ [{step_name}] 点击 ({px}, {py}) 完成")
            time.sleep(wait_after)
            return True

        except Exception as e:
            logger.exception(f"[{step_name}] 第 {attempt} 次出错: {e}")
            time.sleep(config.RETRY_INTERVAL_SEC)

    logger.error(f"[{step_name}] 已重试 {config.MAX_RETRY_TIMES} 次，全部失败！")
    return False


# ============================================================
# 步骤3专用：滚动查找目标座位
# ============================================================

def _find_seat_with_scroll(hwnd: int) -> bool:
    """
    在座位图页面中查找目标座位（自动向下滚动直到找到）。
    - 每次截图后交由大模型判断目标座位是否可见
    - 若模型返回了坐标，先尝试点击
    - 点击后根据弹窗提示大模型【验证】背景放大的座位号码是否选对
    - 选对则继续，选错则点击「取消」并向下滚动重试
    - 最多滚动 MAX_SCROLL_ATTEMPTS 次

    Args:
        hwnd:    已锁定的目标窗口句柄

    Returns:
        bool: 是否成功找到并点击对了目标座位
    """
    MAX_SCROLL_ATTEMPTS = 8   # 最多向下滚动次数（防止无限循环）
    SCROLL_CLICKS = 10        # 每次滚动格数：1格=120，10格相当于滚动较大一段 WebView 内容
    SCROLL_WAIT = 1.2         # 滚动后等待页面重绘的秒数（由于看的是 WebView 需要稍方）

    step_label = "步骤3_查找目标座位"
    wrong_seats = set()  # 保存之前选错的座位编号进行防呆

    import re

    for attempt in range(MAX_SCROLL_ATTEMPTS + 1):
        try:
            scroll_info = f"(已滚动 {attempt} 次)" if attempt > 0 else "(初始视图)"
            logger.info(f"[{step_label}] 第 {attempt + 1} 次搜索 {scroll_info}")

            # 根据历史动态构建目标 Prompt
            avoid_str = ""
            if wrong_seats:
                avoid_str = f"【特别重申：你之前错误地点到了座位 {', '.join(wrong_seats)}，请仔细检查数字，绝对不要再点这些错误的号码！】"

            current_prompt = PROMPT_STEP3_TEMPLATE.format(
                target1=config.PREFERRED_SEATS[0],
                target2=config.PREFERRED_SEATS[1] if len(config.PREFERRED_SEATS) > 1 else "无",
                avoid_str=avoid_str
            )

            # 截图
            img = window_ctrl.capture_window(hwnd)
            _save_screenshot(img, f"{step_label}_scroll{attempt}")

            # 问模型
            raw_text, coord = vision_ai.ask_model(img, current_prompt)

            # 模型明确说不可见，并指示了滚动方向
            upper_raw = (raw_text or "").upper()
            if "SCROLL_UP" in upper_raw:
                logger.info(f"[{step_label}] 模型判断目标在【上方】，执行向上滚动")
                window_ctrl.scroll_window(hwnd, clicks=SCROLL_CLICKS, direction="up")
                time.sleep(SCROLL_WAIT)
            elif "SCROLL_DOWN" in upper_raw or "NOT_VISIBLE" in upper_raw:
                logger.info(f"[{step_label}] 模型判断目标在【下方】(或不可见)，执行向下滚动")
                window_ctrl.scroll_window(hwnd, clicks=SCROLL_CLICKS, direction="down")
                time.sleep(SCROLL_WAIT)
            elif coord is not None:
                # 初步找到了
                px, py = coord
                logger.info(f"[{step_label}] 发现潜在目标坐标=({px}, {py})，尝试点击并进行【二次验证】...")
                window_ctrl.post_click(hwnd, px, py)
                time.sleep(config.STEP_WAIT_SEC)  # 等待弹窗出现和座位放大

                # 截图进行验证
                img_verify = window_ctrl.capture_window(hwnd)
                _save_screenshot(img_verify, f"{step_label}_verify_scroll{attempt}")

                verify_prompt = (
                    "这是点击某个座位后弹出的预约确认界面。在弹窗后方的背景中，被点击的座位通常会放大或高亮显示。"
                    f"请小心辨认背景中当前被选中的座位编号，判断它是否是我们想要的【{config.PREFERRED_SEATS[0]}或{config.PREFERRED_SEATS[1]}】号。\n"
                    "1. 如果【选对了】（编号完全匹配），请只回复：CORRECT\n"
                    "2. 如果【选错了】（编号不是我们的目标），请在第一行回复你实际看到的错误编号（比如回复 WRONG:127），"
                    "然后在第二行返回弹窗左下角「取消」按钮的坐标，格式为 <box>[[x1,y1,x2,y2]]</box>，以便程序回退重选。"
                )
                raw_verify, coord_cancel = vision_ai.ask_model(img_verify, verify_prompt)

                if "CORRECT" in (raw_verify or "").upper():
                    logger.info(f"[{step_label}] ✅ 二次验证成功！确实点中了目标座位。")
                    return True
                elif coord_cancel is not None:
                    cx, cy = coord_cancel
                    
                    # 提取错误编号，记入黑名单
                    m = re.search(r'WRONG:\s*(\d+)', raw_verify, re.IGNORECASE)
                    wrong_num = m.group(1) if m else "未知编号"
                    if wrong_num != "未知编号":
                        wrong_seats.add(wrong_num)
                        logger.warning(f"[{step_label}] 记入防错黑名单：{wrong_num}")

                    logger.warning(f"[{step_label}] ❌ 验证失败（模型选成了 {wrong_num}），正在点击「取消」坐标 ({cx}, {cy})...")
                    window_ctrl.post_click(hwnd, cx, cy)
                    time.sleep(1.0)

                    # 微幅向上滚动 3 格（约 2~3 行）
                    # 目的：打破"同一张截图里反复找同一个错误座位"的死循环
                    # 幅度刻意做小，不改变目标座位的大体位置，让模型只是"换个视角"重找
                    logger.info(f"[{step_label}] 微调视图：向上滚动 3 格...")
                    window_ctrl.scroll_window(hwnd, clicks=3, direction="up")
                    time.sleep(0.6)
                    continue
                else:
                    logger.warning(f"[{step_label}] ⚠️ 验证阶段未得到明确结果（不是CORRECT也无取消坐标），降级为直接放行...")
                    return True
            else:
                logger.warning(f"[{step_label}] 模型未返回有效坐标且无明确方向，默认向下滚动重试")
                window_ctrl.scroll_window(hwnd, clicks=SCROLL_CLICKS, direction="down")
                time.sleep(SCROLL_WAIT)

        except Exception as e:
            logger.exception(f"[{step_label}] 第 {attempt + 1} 次发生异常: {e}")
            time.sleep(config.RETRY_INTERVAL_SEC)

    logger.error(f"[{step_label}] 滚动 {MAX_SCROLL_ATTEMPTS} 次后仍未找到目标座位！")
    return False


# ============================================================
# 主预约流程
# ============================================================

def run_booking(
    hwnd: int,
    dry_run: bool = config.DRY_RUN,
    start_time: str | None = None,
    end_time: str | None = None,
):
    """
    执行完整的自习室预约流程。

    Args:
        hwnd:       已锁定的目标窗口句柄
        dry_run:    测试模式 - 走完步骤1~7，在步骤8「确定」前停下不真正提交
        start_time: 预约开始时间字符串（如 "8:00"），None 则读 config
        end_time:   预约结束时间字符串（如 "22:00"），None 则读 config
    """
    # 时间优先用传入值，否则回落到 config
    _start = start_time or config.BOOKING_START_TIME
    _end   = end_time   or config.BOOKING_END_TIME

    # 动态构建含时间的 Prompt（不能在模块加载时固化，否则 GUI 修改无效）
    prompt_step5 = PROMPT_STEP5_TEMPLATE.format(start_time=_start)
    prompt_step7 = PROMPT_STEP7_TEMPLATE.format(end_time=_end)

    print(f"\n{'='*60}")
    print(f"  🚀 开始自动预约流程  [{'DRY_RUN - 步骤1~7真实点击，步骤8停下' if dry_run else '🔴 正式执行模式'}]")
    print(f"  目标座位: {' -> '.join(config.PREFERRED_SEATS)}")
    print(f"  预约时间: {_start}  ~  {_end}")
    print(f"{'='*60}\n")

    # ---- 步骤 1~2: 导航到座位图 ----
    for step_name, prompt, wait in [
        ("步骤1_点击预约选座Tab", PROMPT_STEP1_BOOKING_TAB,    1.5),
        ("步骤2_点击选座按钮",   PROMPT_STEP2_SELECT_SEAT_BTN, 1.5),
    ]:
        if not _execute_step(hwnd, step_name, prompt, dry_run=False, wait_after=wait):
            print(f"\n  ❌ 流程在 [{step_name}] 中断，请检查日志（screenshots/ 目录）。")
            return False

    # ---- 步骤 3: 滚动查找并点击目标座位（一律真实点击）----
    if not _find_seat_with_scroll(hwnd):
        print("\n  ❌ 流程在 [步骤3_查找目标座位] 中断，请检查日志（screenshots/ 目录）。")
        return False

    # ---- 步骤 4~8: 选择时间并确认 ----
    # 步骤8受 dry_run 控制：DRY_RUN=True 时识别到「确定」按钮坐标后停下，不点击
    for step_name, prompt, is_dry_run, wait in [
        ("步骤4_点击开始时间",   PROMPT_STEP4_START_TIME, False,   1.0),
        ("步骤5_选择开始时间值", prompt_step5,            False,   0.8),
        ("步骤6_点击结束时间",   PROMPT_STEP6_END_TIME_INPUT, False, 1.0),
        ("步骤7_选择结束时间值", prompt_step7,            False,   0.8),
        ("步骤8_点击确定",       PROMPT_STEP8_CONFIRM,    dry_run, 2.0),
    ]:
        if not _execute_step(hwnd, step_name, prompt, dry_run=is_dry_run, wait_after=wait):
            print(f"\n  ❌ 流程在 [{step_name}] 中断，请检查日志（screenshots/ 目录）。")
            return False

    if dry_run:
        print("\n  ✅ 测试完成！已走完步骤1~7，步骤8「确定」已识别坐标但未点击。")
    else:
        print("\n  🎉 预约成功！")
    return True


def _run_steps_1_2(hwnd: int) -> bool:
    """执行步骤1（点击预约选座 Tab）和步骤2（点击选座按钮）"""
    for step_name, prompt, wait in [
        ("步骤1_点击预约选座Tab", PROMPT_STEP1_BOOKING_TAB, 1.5),
        ("步骤2_点击选座按钮",   PROMPT_STEP2_SELECT_SEAT_BTN, 1.5),
    ]:
        if not _execute_step(hwnd, step_name, prompt, dry_run=False, wait_after=wait):
            return False
    return True


def _run_steps_4_to_8(hwnd: int, dry_run: bool) -> bool:
    """执行步骤4~8（时间选择与最终确认）"""
    for step_name, prompt, is_dry_run, wait in [
        ("步骤4_点击开始时间",   PROMPT_STEP4_START_TIME,       False,   1.0),
        ("步骤5_选择开始时间值", PROMPT_STEP5_SELECT_START_TIME, False,   0.8),
        ("步骤6_点击结束时间",   PROMPT_STEP6_END_TIME_INPUT,    False,   1.0),
        ("步骤7_选择结束时间值", PROMPT_STEP7_SELECT_END_TIME,   False,   0.8),
        ("步骤8_点击确定",       PROMPT_STEP8_CONFIRM,           dry_run, 2.0),
    ]:
        if not _execute_step(hwnd, step_name, prompt, dry_run=is_dry_run, wait_after=wait):
            return False
    return True
