"""
主入口 (GUI 界面) - 自习室自动预约机器人
"""
import sys
import time
import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import win32api
import win32gui
import win32con
from datetime import datetime, timedelta

import config
import window_ctrl
import booking_flow

# ============================================================
# 日志配置（同时输出到控制台、文件和 GUI）
# ============================================================
class TextHandler(logging.Handler):
    """将日志输出到 Tkinter 的 Text 控件"""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + "\n")
            self.text_widget.configure(state='disabled')
            self.text_widget.yview(tk.END)
        # 确保在主线程更新 GUI
        self.text_widget.after(0, append)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("auto_booking.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("main")


class BookingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("自习室自动预约机器人 v1.1 - GUI 版")
        self.geometry("580x620")
        self.target_hwnd = None
        self.running = False  # 是否正在执行任务

        self._setup_ui()
        self._check_config()
        logger.info("程序启动，请先锁定目标窗口。")

    def _check_config(self):
        """检查必要配置，如果缺失则引导用户"""
        if not config.API_KEY or config.API_KEY == "在这里输入您的API密钥":
            msg = (
                "检测到未设置 API 密钥！\n\n"
                "引导步骤：\n"
                "1. 在项目根目录找到 .env.example 文件\n"
                "2. 将其复制并重命名为 .env\n"
                "3. 打开 .env 文件，填入您的阿里云 DashScope API Key\n\n"
                "如果不设置密钥，视觉识别功能将无法工作。"
            )
            messagebox.showwarning("缺少配置", msg)
            logger.warning("未检测到 API_KEY，请按照弹窗指引配置 .env 文件。")

    def _setup_ui(self):
        style = ttk.Style(self)
        style.theme_use('clam')

        # 顶部信息取
        info_frame = ttk.LabelFrame(self, text="配置信息", padding=10)
        info_frame.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(info_frame, text=f"模型: {config.MODEL_NAME}").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(info_frame, text=f"座位: {' -> '.join(config.PREFERRED_SEATS)}").grid(row=0, column=1, sticky=tk.W, padx=20)
        ttk.Label(info_frame, text=f"触发时间: {config.TRIGGER_HOUR:02d}:{config.TRIGGER_MINUTE:02d}").grid(row=1, column=0, sticky=tk.W, pady=5)
        ttk.Label(info_frame, text=f"预约时段: {config.BOOKING_START_TIME} - {config.BOOKING_END_TIME}").grid(row=1, column=1, sticky=tk.W, padx=20, pady=5)

        # 窗口锁定区
        win_frame = ttk.LabelFrame(self, text="目标窗口", padding=10)
        win_frame.pack(fill=tk.X, padx=10, pady=5)

        self.lbl_window = ttk.Label(win_frame, text="【未锁定】请点击下方按钮选择", foreground="red", font=("Microsoft YaHei", 10, "bold"))
        self.lbl_window.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_select = ttk.Button(win_frame, text="🎯 手动点击锁定窗口", command=self.start_window_selection)
        self.btn_select.pack(side=tk.RIGHT)

        # 预约时间配置区
        time_frame = ttk.LabelFrame(self, text="预约时间", padding=10)
        time_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(time_frame, text="开始时间:").grid(row=0, column=0, sticky=tk.W)
        self.ent_start = ttk.Entry(time_frame, width=10)
        self.ent_start.insert(0, config.BOOKING_START_TIME)
        self.ent_start.grid(row=0, column=1, sticky=tk.W, padx=(4, 20))

        ttk.Label(time_frame, text="结束时间:").grid(row=0, column=2, sticky=tk.W)
        self.ent_end = ttk.Entry(time_frame, width=10)
        self.ent_end.insert(0, config.BOOKING_END_TIME)
        self.ent_end.grid(row=0, column=3, sticky=tk.W, padx=4)

        ttk.Label(time_frame, text="（示例：8:00 和 22:00）",
                  foreground="gray").grid(row=0, column=4, sticky=tk.W, padx=10)

        # 操作区
        act_frame = ttk.LabelFrame(self, text="操作", padding=10)
        act_frame.pack(fill=tk.X, padx=10, pady=5)

        self.btn_test = ttk.Button(act_frame, text="🛠️ 立即测试 (步骤1~7真实点,步骤8停下)", command=lambda: self.start_task(dry_run=True, schedule=False))
        self.btn_test.pack(fill=tk.X, padx=5, pady=2)

        btn_row2 = ttk.Frame(act_frame)
        btn_row2.pack(fill=tk.X, pady=2)
        self.btn_real = ttk.Button(btn_row2, text="🔴 立即正式预约", command=lambda: self.start_task(dry_run=False, schedule=False))
        self.btn_real.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        self.btn_schedule = ttk.Button(btn_row2, text="⏰ 开始定时自动预约 (正式)", command=lambda: self.start_task(dry_run=False, schedule=True))
        self.btn_schedule.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        # 日志输出区
        log_frame = ttk.LabelFrame(self, text="运行日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.txt_log = scrolledtext.ScrolledText(log_frame, state='disabled', font=("Consolas", 9))
        self.txt_log.pack(fill=tk.BOTH, expand=True)
        
        # 将日志绑定到 Text 控件
        gui_handler = TextHandler(self.txt_log)
        gui_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(gui_handler)

    # ------------------ 窗口抓取逻辑 ------------------
    def start_window_selection(self):
        if self.running: return
        self.target_hwnd = None
        self.lbl_window.config(text="请移动鼠标并【单击】微信小程序窗口...", foreground="blue")
        self.btn_select.config(state=tk.DISABLED)
        logger.info("等待您单击目标窗口...")
        self._wait_mouse_release()

    def _wait_mouse_release(self):
        """等待上一次点击（点按钮的点击）松开"""
        if win32api.GetAsyncKeyState(win32con.VK_LBUTTON) < 0:
            self.after(50, self._wait_mouse_release)
        else:
            self.after(100, self._wait_mouse_click)

    def _wait_mouse_click(self):
        """轮询等待下一次鼠标单击"""
        if win32api.GetAsyncKeyState(win32con.VK_LBUTTON) < 0:
            # 拿到坐标
            x, y = win32api.GetCursorPos()
            # 获取最初拿到的句柄
            hwnd = win32gui.WindowFromPoint((x, y))
            # 向上找到顶层父窗口
            root_hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
            
            # 过滤掉桌面本身或自身重合
            title = win32gui.GetWindowText(root_hwnd).strip()
            rect = win32gui.GetWindowRect(root_hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]

            self.target_hwnd = root_hwnd
            title_disp = title if title else "无标题"
            self.lbl_window.config(
                text=f"【已锁定】{title_disp} (句柄: {root_hwnd}, 尺寸: {w}x{h})", 
                foreground="green"
            )
            self.btn_select.config(state=tk.NORMAL)
            logger.info(f"成功锁定窗口: {title_disp} ({w}x{h}) HWND={root_hwnd}")
            
            # 如果不小心选到了任务栏（高度极小但宽度极宽），给个警告
            if w > 1920 and h <= 50:
                logger.warning("⚠️ 警告：您选中的窗口极宽且极矮，可能是 Windows 任务栏！请重新选择微信小程序窗口。")
                messagebox.showwarning("选中错误的窗口？", "您似乎选到了任务栏。请关闭此对话框后，点击【重新选择窗口】，然后明确地点在微信小程序界面中间。")
        else:
            # 继续轮询
            self.after(50, self._wait_mouse_click)


    # ------------------ 任务控制逻辑 ------------------
    def set_buttons_state(self, state):
        self.btn_select.config(state=state)
        self.btn_test.config(state=state)
        self.btn_real.config(state=state)
        self.btn_schedule.config(state=state)
        self.ent_start.config(state=state)
        self.ent_end.config(state=state)

    def start_task(self, dry_run: bool, schedule: bool):
        if self.target_hwnd is None:
            messagebox.showerror("未锁定窗口", "请先点击【🎯 手动点击锁定窗口】，并在微信小程序内单击确认！")
            return
        if self.running:
            return

        self.running = True
        self.set_buttons_state(tk.DISABLED)
        
        # 启动后台线程执行，避免卡死 GUI
        threading.Thread(target=self._task_thread, args=(dry_run, schedule), daemon=True).start()

    def _task_thread(self, dry_run: bool, use_schedule: bool):
        try:
            if use_schedule:
                self._wait_until_trigger()

            logger.info("=" * 40)
            logger.info(f"开始执行预约流程 [{'DRY_RUN(测试)' if dry_run else '正式提交'}]")

            # 读取 GUI 里用户设置的时间
            start_time = self.ent_start.get().strip() or config.BOOKING_START_TIME
            end_time   = self.ent_end.get().strip()   or config.BOOKING_END_TIME
            logger.info(f"预约时间段: {start_time} ~ {end_time}")

            success = booking_flow.run_booking(
                self.target_hwnd,
                dry_run=dry_run,
                start_time=start_time,
                end_time=end_time,
            )

            if success:
                logger.info("✅ 流程正常结束！")
                messagebox.showinfo("成功", "✅ 预约流程顺利完成（测试模式请检查日志，正式模式请确认结果）！")
            else:
                logger.error("❌ 流程抛锚，请查看 screenshots/ 目录排查 AI 识别失败在哪一步。")
                messagebox.showerror("失败", "❌ 流程终止，可能有截图未正确识别，详见日志！")
        except booking_flow.BookingError as be:
            logger.warning(f"业务中断: {be}")
            messagebox.showwarning("预约失败", str(be))
        except Exception as e:
            logger.exception("任务异常中断")
            messagebox.showerror("系统异常", f"运行中发生未知错误:\n{e}")
        finally:
            self.running = False
            # 恢复 GUI 按钮
            self.after(0, lambda: self.set_buttons_state(tk.NORMAL))

    def _wait_until_trigger(self):
        """阻塞等待至明早或今早 6:00 """
        target = datetime.now().replace(
            hour=config.TRIGGER_HOUR, 
            minute=config.TRIGGER_MINUTE, 
            second=0, microsecond=0
        )
        if datetime.now() >= target:
            target += timedelta(days=1)

        logger.info(f"⏰ 进入定时模式，等待至 {target.strftime('%H:%M:%S')} ...")
        
        preheat_target = target - timedelta(seconds=10)
        
        # 长时间等待循环（带阻断检查，方便以后如果做退出功能）
        while True:
            now = datetime.now()
            if now >= preheat_target:
                break
            time.sleep(1)
            
        logger.info("🔥 进入倒计时 10 秒预热阶段...")
        while datetime.now() < target:
            time.sleep(0.01)

if __name__ == "__main__":
    app = BookingApp()
    app.mainloop()
