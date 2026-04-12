"""
窗口控制模块 - 负责：
1. 让用户用鼠标点选目标窗口（锁定句柄）
2. 后台截图（无需激活窗口，无需移动鼠标）
3. 后台发送点击事件（向指定 HWND 发消息）
"""
import time
import ctypes
import ctypes.wintypes
import win32gui
import win32con
import win32api
import win32ui
from PIL import Image
import io

# Windows API 常量
PW_RENDERFULLCONTENT = 0x00000002  # PrintWindow 渲染完整内容（包括 WebView）


def pick_window_by_click(prompt: str = "请在 3 秒内单击要控制的窗口...") -> int:
    """
    弹出提示，等待用户鼠标点击，返回被点击位置的顶层窗口 HWND。

    Returns:
        int: 目标窗口 HWND（句柄）
    """
    print(f"\n{'='*60}")
    print(f"  {prompt}")
    print(f"{'='*60}")

    # 等待鼠标左键松开（确保用户已停止上一次操作）
    print("  等待你的手离开鼠标...")
    while win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000:
        time.sleep(0.05)

    print("  请在 5 秒内单击目标窗口... ", end="", flush=True)
    deadline = time.time() + 5.0
    clicked = False
    while time.time() < deadline:
        if win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000:
            # 检测到左键按下
            x, y = win32api.GetCursorPos()
            # 等待松开再获取窗口（避免获取到按钮控件而不是顶层窗口）
            time.sleep(0.15)
            hwnd = win32gui.WindowFromPoint((x, y))
            # 获取最顶层父窗口
            root_hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
            clicked = True
            break
        time.sleep(0.02)

    if not clicked:
        raise TimeoutError("超时：未检测到鼠标点击，程序退出。")

    title = win32gui.GetWindowText(root_hwnd)
    rect = win32gui.GetWindowRect(root_hwnd)
    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    print(f"\n  ✅ 已锁定窗口：「{title}」  句柄={root_hwnd}  尺寸={w}x{h}")
    return root_hwnd


def capture_window(hwnd: int) -> Image.Image:
    """
    使用 PrintWindow API 后台截取指定 HWND 的完整画面，即使窗口被遮挡或最小化。
    适用于微信小程序的 WebView 内容。

    Args:
        hwnd: 目标窗口句柄

    Returns:
        PIL.Image 对象（RGB 模式）
    """
    # 获取窗口客户区尺寸
    rect = win32gui.GetClientRect(hwnd)
    w, h = rect[2], rect[3]
    if w == 0 or h == 0:
        raise ValueError(f"窗口 {hwnd} 尺寸为 0，可能已最小化或已关闭。")

    # 创建设备上下文
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    # 创建位图
    save_bitmap = win32ui.CreateBitmap()
    save_bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(save_bitmap)

    # 调用 PrintWindow 渲染内容（PW_RENDERFULLCONTENT = 2 可渲染 WebView/分层窗口）
    result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)

    # 转换为 PIL Image
    bmp_info = save_bitmap.GetInfo()
    bmp_str = save_bitmap.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB",
        (bmp_info["bmWidth"], bmp_info["bmHeight"]),
        bmp_str, "raw", "BGRX", 0, 1
    )

    # 释放资源
    win32gui.DeleteObject(save_bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)

    if result == 0:
        # PrintWindow 返回 0 表示失败，回退到 BitBlt 截图
        raise RuntimeError("PrintWindow 失败，请确认窗口未被完全最小化。")

    return img


def post_click(hwnd: int, x: int, y: int, delay_ms: int = 50):
    """
    向指定 HWND 的客户区坐标 (x, y) 发送后台鼠标点击消息（不移动真实鼠标）。

    Args:
        hwnd: 目标窗口句柄（客户区相对坐标）
        x: 客户区横坐标
        y: 客户区纵坐标
        delay_ms: 按下和抬起之间的延时（毫秒）
    """
    lParam = win32api.MAKELONG(x, y)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lParam)
    time.sleep(delay_ms / 1000.0)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lParam)


def get_window_client_size(hwnd: int) -> tuple[int, int]:
    """返回窗口客户区 (width, height)"""
    rect = win32gui.GetClientRect(hwnd)
    return rect[2], rect[3]


def bring_window_to_foreground(hwnd: int):
    """将窗口带到前台（仅在测试时使用，确认截图是否正确）"""
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)


WHEEL_DELTA = 120  # Windows 标准滚轮单位
MOUSEEVENTF_WHEEL = 0x0800  # mouse_event 滚轮标志位


def scroll_window(hwnd: int, clicks: int = 10, direction: str = "down"):
    """
    滚动指定窗口内容（驱动层注入，适用于 WebView / Chromium 内核应用）。

    核心原理：
    1. 临时将鼠标光标移到目标窗口中心（屏幕坐标）
    2. 调用 win32api.mouse_event 注入滚轮事件（驱动层，无法被过滤）
    3. 恢复鼠标到原位置
    整个过程约 150ms，对用户基本无感。

    Args:
        hwnd:      目标窗口句柄
        clicks:    滚动格数（每格 = WHEEL_DELTA = 120），越大滚越多
        direction: "down" 向下 / "up" 向上
    """
    # 获取窗口在屏幕上的中心坐标
    rect = win32gui.GetWindowRect(hwnd)  # 返回屏幕绝对坐标
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2

    # 保存当前鼠标位置（操作完后恢复）
    old_pos = win32api.GetCursorPos()

    try:
        # 把鼠标移到目标窗口中心
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.05)  # 等待 OS 感知光标已在此位置

        # 计算滚轮 delta（负数 = 向下，正数 = 向上）
        wheel_delta = -WHEEL_DELTA * clicks if direction == "down" else WHEEL_DELTA * clicks

        # 驱动层注入滚轮事件（mouse_event，比 PostMessage/SendMessage 更底层）
        win32api.mouse_event(MOUSEEVENTF_WHEEL, cx, cy, wheel_delta, 0)
        time.sleep(0.08)  # 等待 WebView 处理滚轮事件

    finally:
        # 无论是否异常，都恢复鼠标原位
        win32api.SetCursorPos(old_pos)

