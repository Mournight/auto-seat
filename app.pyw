"""
主入口 (GUI 界面) - 自习室自动预约机器人 
新增：AI 提示词配置标签页，支持实时编辑、保存和恢复默认。
"""
import os
# 强制开启 Python 的 UTF-8 模式，解决 Windows 常见的 ASCII 编码报错
os.environ["PYTHONUTF8"] = "1"

import sys
import io
import time
import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import win32api
import win32gui
import win32con
import ctypes
from datetime import datetime, timedelta

import config
import window_ctrl
import booking_flow
import agent  # 用于提示词的 load_prompt / save_prompt / DEFAULT_SYSTEM_PROMPT

# 仅在有真实控制台（python.exe）时才重定向输出流编码；
# pythonw.exe 运行时 sys.stdout/stderr 为 None，须跳过，否则会静默闪退。
if sys.platform == "win32" and sys.stdout is not None and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.platform == "win32" and sys.stderr is not None and hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

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
# 抑制 httpx 和 httpcore 的调试日志，防止其在尝试输出含中文的请求包时因编码问题崩溃
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("main")


class BookingApp(tk.Tk):
    def __init__(self):
        # 尝试开启 Windows 高分屏适配（防止模糊）
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        super().__init__()
        self.title("自习室自动预约 bot")
        self.attributes("-topmost", True)  # 窗口置顶
        self.geometry("680x850")
        self.minsize(600, 750)
        self.target_hwnd = None
        self.running = False  # 是否正在执行任务
        self.task_cancelled = False  # 任务取消标记

        self._setup_ui()
        logger.info("程序启动，请先锁定目标窗口。")

    # ============================================================
    # UI 构建
    # ============================================================
    def _setup_ui(self):
        style = ttk.Style(self)
        style.theme_use('clam')

        # ---- 主 Notebook（标签页） ----
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))

        self.tab_main   = ttk.Frame(self.notebook, padding=4)
        self.tab_prompt = ttk.Frame(self.notebook, padding=4)

        self.notebook.add(self.tab_main,   text="🤖  预约控制")
        self.notebook.add(self.tab_prompt, text="📝  提示词配置")

        self._setup_main_tab()
        self._setup_prompt_tab()

        # ---- 日志区（在 Notebook 外、全局共享）----
        log_frame = ttk.LabelFrame(self, text="运行日志", padding=6)
        log_frame.pack(fill=tk.X, expand=False, padx=8, pady=(0, 8))

        self.txt_log = scrolledtext.ScrolledText(
            log_frame, state='disabled',
            font=("Consolas", 9), height=12
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # 将日志绑定到 Text 控件
        gui_handler = TextHandler(self.txt_log)
        gui_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(gui_handler)

    # ---- 预约控制标签页 ----
    def _setup_main_tab(self):
        frame = self.tab_main

        # 顶部配置信息
        info_frame = ttk.LabelFrame(frame, text="当前配置", padding=8)
        info_frame.pack(fill=tk.X, pady=(4, 4))

        ttk.Label(info_frame, text=f"模型: {config.MODEL_NAME}").grid(row=0, column=0, sticky=tk.W)
        self.lbl_seat_config = ttk.Label(info_frame, text=f"座位: {config.get_seat_display_text()}")
        self.lbl_seat_config.grid(row=0, column=1, sticky=tk.W, padx=20)

        thinking_text = "思考模式: ✅ 开启" if config.AGENT_ENABLE_THINKING else "思考模式: ⚡ 关闭(快速)"
        ttk.Label(info_frame, text=thinking_text, foreground="#2a7a2a" if config.AGENT_ENABLE_THINKING else "#888").grid(
            row=0, column=2, sticky=tk.W, padx=10)

        ttk.Label(info_frame, text=f"触发时间: {config.TRIGGER_TIME}").grid(
            row=1, column=0, sticky=tk.W, pady=4)
        ttk.Label(info_frame, text=f"默认时段: {config.BOOKING_START_TIME} - {config.BOOKING_END_TIME}").grid(
            row=1, column=1, sticky=tk.W, padx=20, pady=4)

        # ---- 醒目警告提示 ----
        warning_msg = (
            "⚠️ 重要提示：请先将目标窗口尽量放大（窗口越大，识别通常越稳定）；"
            "运行期间务必保持目标窗口在前台、不要最小化，并尽量不要人为操作鼠标。"
        )
        self.lbl_warning = tk.Label(
            frame, text=warning_msg, wraplength=550,
            foreground="red", font=("Microsoft YaHei", 9, "bold"),
            justify=tk.LEFT, pady=8
        )
        self.lbl_warning.pack(fill=tk.X)

        # 窗口锁定区
        win_frame = ttk.LabelFrame(frame, text="目标窗口", padding=8)
        win_frame.pack(fill=tk.X, pady=4)

        self.lbl_window = ttk.Label(
            win_frame, text="【未锁定】请点击下方按钮选择",
            foreground="red", font=("Microsoft YaHei", 10, "bold")
        )
        self.lbl_window.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_select = ttk.Button(
            win_frame, text="🎯 手动点击锁定窗口",
            command=self.start_window_selection
        )
        self.btn_select.pack(side=tk.RIGHT)

        ttk.Label(
            win_frame,
            text="建议：锁定前先把目标窗口尽量放大，可明显提升识别准确率",
            foreground="#b05a00"
        ).pack(side=tk.BOTTOM, anchor=tk.W, pady=(6, 0))

        # 预约时间配置区
        time_frame = ttk.LabelFrame(frame, text="预约参数设置", padding=8)
        time_frame.pack(fill=tk.X, pady=4)

        # 第一行：抢座触发时间
        ttk.Label(time_frame, text="1. 抢座开始时间:").grid(row=0, column=0, sticky=tk.W)
        self.ent_trigger = ttk.Entry(time_frame, width=12)
        self.ent_trigger.insert(0, config.TRIGGER_TIME)
        self.ent_trigger.grid(row=0, column=1, sticky=tk.W, padx=4)
        ttk.Label(time_frame, text=" (每日触发抢座的时刻，如 06:00)", foreground="gray").grid(row=0, column=2, sticky=tk.W)

        # 第二行：预约时间段
        ttk.Label(time_frame, text="2. 预约时段设置:").grid(row=1, column=0, sticky=tk.W, pady=4)
        
        time_inner = ttk.Frame(time_frame)
        time_inner.grid(row=1, column=1, columnspan=2, sticky=tk.W)

        self.ent_start = ttk.Entry(time_inner, width=8)
        self.ent_start.insert(0, config.BOOKING_START_TIME)
        self.ent_start.pack(side=tk.LEFT, padx=4)
        
        ttk.Label(time_inner, text="至").pack(side=tk.LEFT)
        
        self.ent_end = ttk.Entry(time_inner, width=8)
        self.ent_end.insert(0, config.BOOKING_END_TIME)
        self.ent_end.pack(side=tk.LEFT, padx=4)
        
        ttk.Label(time_inner, text="(如 08:00 到 22:00)", foreground="gray").pack(side=tk.LEFT, padx=10)

        # 第三行：座位策略二选一
        ttk.Label(time_frame, text="3. 目标座位策略:").grid(row=2, column=0, sticky=tk.W, pady=4)

        seat_mode_frame = ttk.Frame(time_frame)
        seat_mode_frame.grid(row=2, column=1, columnspan=2, sticky=tk.W)

        init_mode = config.SEAT_MODE if config.SEAT_MODE in ("list", "range") else "list"
        self.seat_mode_var = tk.StringVar(value=init_mode)
        self.rb_seat_mode_list = ttk.Radiobutton(
            seat_mode_frame,
            text="具体座位（优先级列表）",
            value="list",
            variable=self.seat_mode_var,
            command=self._on_seat_mode_change,
        )
        self.rb_seat_mode_list.pack(side=tk.LEFT, padx=(0, 10))

        self.rb_seat_mode_range = ttk.Radiobutton(
            seat_mode_frame,
            text="连续座位段（起止范围）",
            value="range",
            variable=self.seat_mode_var,
            command=self._on_seat_mode_change,
        )
        self.rb_seat_mode_range.pack(side=tk.LEFT)

        # 第四行：具体座位列表
        ttk.Label(time_frame, text="4. 具体座位:").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.ent_seat_list = ttk.Entry(time_frame, width=40)
        self.ent_seat_list.insert(0, ",".join(config.PREFERRED_SEATS))
        self.ent_seat_list.grid(row=3, column=1, sticky=tk.W, padx=4)
        ttk.Label(time_frame, text="逗号分隔，如 120,121,122", foreground="gray").grid(row=3, column=2, sticky=tk.W)

        # 第五行：连续座位段
        ttk.Label(time_frame, text="5. 座位段:").grid(row=4, column=0, sticky=tk.W, pady=4)
        seat_range_frame = ttk.Frame(time_frame)
        seat_range_frame.grid(row=4, column=1, columnspan=2, sticky=tk.W)

        self.ent_seat_range_start = ttk.Entry(seat_range_frame, width=8)
        self.ent_seat_range_start.insert(0, config.SEAT_RANGE_START)
        self.ent_seat_range_start.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(seat_range_frame, text="到").pack(side=tk.LEFT)
        self.ent_seat_range_end = ttk.Entry(seat_range_frame, width=8)
        self.ent_seat_range_end.insert(0, config.SEAT_RANGE_END)
        self.ent_seat_range_end.pack(side=tk.LEFT, padx=4)
        ttk.Label(seat_range_frame, text="例如 120 到 150", foreground="gray").pack(side=tk.LEFT, padx=8)

        self._on_seat_mode_change()

        # 第六行：保存默认值
        self.btn_save_config = ttk.Button(
            time_frame, text="💾 保存并设置为默认",
            command=self._save_config_to_env
        )
        self.btn_save_config.grid(row=5, column=1, columnspan=2, sticky=tk.E, pady=(6, 0))

        # 操作区
        act_frame = ttk.LabelFrame(frame, text="操作", padding=8)
        act_frame.pack(fill=tk.X, pady=4)

        self.btn_test = ttk.Button(
            act_frame, text="🛠️ 立即测试 (手动触发一次流程，最后一步不提交)",
            command=lambda: self.start_task(dry_run=True, schedule=False)
        )
        self.btn_test.pack(fill=tk.X, padx=4, pady=4)

        self.btn_schedule = ttk.Button(
            act_frame, text="⏰ 开始定时模式 (每日自动抢座)",
            command=lambda: self.start_task(dry_run=False, schedule=True)
        )
        self.btn_schedule.pack(fill=tk.X, padx=4, pady=4)

        self.btn_stop = ttk.Button(
            act_frame, text="⏹️ 停止并退出当前任务 (包括定时等待)",
            command=self.stop_task,
            state=tk.DISABLED
        )
        self.btn_stop.pack(fill=tk.X, padx=4, pady=(2, 4))

        self.btn_clear = ttk.Button(
            act_frame, text="🧹 清理任务截图缓存 (screenshots 目录)",
            command=self._clear_screenshots
        )
        self.btn_clear.pack(fill=tk.X, padx=4, pady=4)

    # ---- 提示词配置标签页 ----
    def _setup_prompt_tab(self):
        frame = self.tab_prompt

        # 说明提示
        hint = (
            "在下方编辑 AI 的系统提示词（System Prompt）。\n"
            "提示词中可使用以下占位符，运行时会自动替换：\n"
            "  {seat1}/{seat2} → 前两个目标座位（兼容旧模板）\n"
            "  {seat_targets}  → 完整目标座位列表/座位段展开列表\n"
            "  {seat_mode_desc} → 当前座位策略说明（列表或座位段）\n"
            "  {start_time} → 预约开始时间     {end_time} → 预约结束时间\n"
            "• 不同学校/楼栋的座位图布局不同，请根据实际情况修改步骤描述。\n"
            "• 保存后下次点击「预约」时立刻生效，无需重启程序。"
        )
        lbl_hint = ttk.Label(
            frame, text=hint, justify=tk.LEFT,
            foreground="#444", font=("Microsoft YaHei", 9),
            relief="groove", padding=8
        )
        lbl_hint.pack(fill=tk.X, pady=(4, 6))

        # 提示词文本编辑器
        self.txt_prompt = scrolledtext.ScrolledText(
            frame, font=("Consolas", 11),
            wrap=tk.WORD, undo=True, height=25
        )
        self.txt_prompt.pack(fill=tk.BOTH, expand=True)

        # 加载当前提示词
        current_prompt = agent.load_prompt()
        self.txt_prompt.insert("1.0", current_prompt)

        # 按钮行
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(6, 4))

        ttk.Button(
            btn_frame, text="💾  保存提示词",
            command=self._save_prompt
        ).pack(side=tk.LEFT, padx=4)

        ttk.Button(
            btn_frame, text="🔄  恢复内置默认",
            command=self._reset_prompt
        ).pack(side=tk.LEFT, padx=4)

        # 右侧文件路径提示
        ttk.Label(
            btn_frame,
            text=f"保存路径: system_prompt.txt",
            foreground="gray", font=("Microsoft YaHei", 8)
        ).pack(side=tk.RIGHT, padx=8)

    def _save_prompt(self):
        """保存提示词编辑器中的内容到文件"""
        text = self.txt_prompt.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("提示", "提示词不能为空！")
            return
        if agent.save_prompt(text):
            messagebox.showinfo("保存成功", "✅ 提示词已保存！\n下次点击预约按钮时立刻生效。")
            logger.info("用户已保存自定义提示词")
        else:
            messagebox.showerror("保存失败", "❌ 写入文件失败，请检查目录权限。")

    def _reset_prompt(self):
        """恢复内置默认提示词（覆盖编辑器内容，并自动保存）"""
        if not messagebox.askyesno(
            "确认恢复", "此操作将用内置默认提示词覆盖编辑器中的内容，是否继续？"
        ):
            return
        self.txt_prompt.delete("1.0", tk.END)
        self.txt_prompt.insert("1.0", agent.DEFAULT_SYSTEM_PROMPT)
        # 同时删除文件，让 load_prompt 下次退回默认
        if os.path.exists(agent.PROMPT_FILE):
            try:
                os.remove(agent.PROMPT_FILE)
            except Exception as e:
                logger.warning(f"删除提示词文件失败: {e}")
        logger.info("已恢复内置默认提示词")
        messagebox.showinfo("已恢复", "✅ 已恢复内置默认提示词。")

    # ============================================================
    # 窗口抓取逻辑
    # ============================================================
    def start_window_selection(self):
        if self.running:
            return
        self.target_hwnd = None
        self.lbl_window.config(text="请移动鼠标并【单击】预约程序窗口...", foreground="blue")
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
            x, y = win32api.GetCursorPos()
            hwnd = win32gui.WindowFromPoint((x, y))
            root_hwnd = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)

            title = win32gui.GetWindowText(root_hwnd).strip()
            rect = win32gui.GetWindowRect(root_hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]

            self.target_hwnd = root_hwnd
            title_disp = title if title else "无标题"
            self.lbl_window.config(
                text=f"【已锁定 - 请确认窗口标题是否和预约窗口一致】{title_disp} (句柄: {root_hwnd}, 尺寸: {w}x{h})",
                foreground="green"
            )
            self.btn_select.config(state=tk.NORMAL)
            logger.info(f"成功锁定窗口: {title_disp} ({w}x{h}) HWND={root_hwnd}")

            if w < 900 or h < 700:
                logger.warning("⚠️ 目标窗口尺寸偏小，建议尽量放大窗口以提高识别准确率。")

            if w > 1920 and h <= 50:
                logger.warning("⚠️ 警告：您选中的窗口极宽且极矮，可能是 Windows 任务栏！")
                messagebox.showwarning("选中错误的窗口？",
                    "您似乎选到了任务栏。请关闭此对话框后，点击【重新选择窗口】，\n"
                    "然后明确地点在微信小程序界面中间。")
        else:
            self.after(50, self._wait_mouse_click)

    def _parse_seat_list_input(self, text: str) -> list[str]:
        """解析具体座位输入，支持中英文逗号和空格分隔，并去重保序。"""
        normalized = text.replace("，", ",").replace("、", ",").replace(" ", ",")
        parts = [x.strip() for x in normalized.split(",") if x.strip()]
        uniq: list[str] = []
        seen: set[str] = set()
        for p in parts:
            if p not in seen:
                uniq.append(p)
                seen.add(p)
        return uniq

    def _build_seat_display_text(self, mode: str, seats: list[str], range_start: str, range_end: str) -> str:
        """构建顶部“当前配置”中的座位摘要文案。"""
        if mode == "range":
            if range_start and range_end:
                return f"座位段: {range_start}-{range_end}"
            return "座位段: 未完整配置"
        if not seats:
            return "座位列表: 未配置"
        return f"座位列表: {' -> '.join(seats)}"

    def _on_seat_mode_change(self):
        """切换座位策略时，仅启用当前模式对应的输入框。"""
        mode = self.seat_mode_var.get() if hasattr(self, "seat_mode_var") else "list"
        if mode == "range":
            self.ent_seat_list.config(state=tk.DISABLED)
            self.ent_seat_range_start.config(state=tk.NORMAL)
            self.ent_seat_range_end.config(state=tk.NORMAL)
        else:
            self.ent_seat_list.config(state=tk.NORMAL)
            self.ent_seat_range_start.config(state=tk.DISABLED)
            self.ent_seat_range_end.config(state=tk.DISABLED)

    def _save_config_to_env(self):
        """将当前输入的配置保存到本地 .env 文件"""
        trigger_v = self.ent_trigger.get().strip()
        start_v = self.ent_start.get().strip()
        end_v = self.ent_end.get().strip()
        mode = self.seat_mode_var.get().strip().lower() if hasattr(self, "seat_mode_var") else "list"

        if mode not in ("list", "range"):
            messagebox.showwarning("提示", "座位策略无效，请在“具体座位”和“座位段”中二选一。")
            return

        if not all([trigger_v, start_v, end_v]):
            messagebox.showwarning("提示", "所有参数均不能为空！")
            return
        
        # 简单校验格式
        if ":" not in trigger_v or ":" not in start_v or ":" not in end_v:
            messagebox.showwarning("格式错误", "时间格式应为 HH:MM (如 06:00)")
            return

        list_backup = self._parse_seat_list_input(self.ent_seat_list.get().strip())
        seat_list_value = ",".join(list_backup) if list_backup else ""
        range_start_v = self.ent_seat_range_start.get().strip()
        range_end_v = self.ent_seat_range_end.get().strip()

        if mode == "list":
            seats = self._parse_seat_list_input(self.ent_seat_list.get().strip())
            if not seats:
                messagebox.showwarning("座位配置错误", "请至少填写一个具体座位（支持逗号分隔多个）。")
                return
            seat_list_value = ",".join(seats)
            seat_display = self._build_seat_display_text("list", seats, "", "")
        else:
            if not (range_start_v.isdigit() and range_end_v.isdigit()):
                messagebox.showwarning("座位配置错误", "座位段必须是数字，如 120 到 150。")
                return

            start_num = int(range_start_v)
            end_num = int(range_end_v)
            if start_num > end_num:
                messagebox.showwarning("座位配置错误", "座位段起始值不能大于结束值。")
                return

            span = end_num - start_num + 1
            if span > config.MAX_SEAT_RANGE_SPAN:
                messagebox.showwarning(
                    "座位配置错误",
                    f"座位段过长（{span} 个），请控制在 {config.MAX_SEAT_RANGE_SPAN} 个以内。"
                )
                return

            range_start_v = str(start_num)
            range_end_v = str(end_num)
            seat_display = self._build_seat_display_text("range", [], range_start_v, range_end_v)

        env_path = ".env"
        if not os.path.exists(env_path):
            with open(env_path, "w", encoding="utf-8") as f: f.write("")

        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            keys = {
                "TRIGGER_TIME": trigger_v,
                "BOOKING_START_TIME": start_v,
                "BOOKING_END_TIME": end_v,
                "SEAT_MODE": mode,
                "PREFERRED_SEATS": seat_list_value,
                "SEAT_RANGE_START": range_start_v,
                "SEAT_RANGE_END": range_end_v,
            }
            
            new_lines = []
            found_keys = set()

            for line in lines:
                l_key = line.split('=')[0].strip() if '=' in line else None
                if l_key in keys:
                    new_lines.append(f'{l_key}="{keys[l_key]}"\n')
                    found_keys.add(l_key)
                else:
                    new_lines.append(line)

            for k, v in keys.items():
                if k not in found_keys:
                    new_lines.append(f'{k}="{v}"\n')

            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            # 更新运行时配置，避免用户保存后顶部“当前配置”不刷新
            config.TRIGGER_TIME = trigger_v
            config.BOOKING_START_TIME = start_v
            config.BOOKING_END_TIME = end_v
            config.SEAT_MODE = mode
            if list_backup:
                config.PREFERRED_SEATS = list_backup
            if mode == "list":
                config.PREFERRED_SEATS = self._parse_seat_list_input(seat_list_value)
            config.SEAT_RANGE_START = range_start_v
            config.SEAT_RANGE_END = range_end_v

            if hasattr(self, "lbl_seat_config"):
                self.lbl_seat_config.config(text=f"座位: {seat_display}")

            logger.info(f"配置已保存到 .env 文件（座位策略：{seat_display}）")
            messagebox.showinfo("保存成功", f"✅ 配置已永久保存！\n当前座位策略：{seat_display}\n下次启动将默认使用。")
        except Exception as e:
            logger.error(f"保存失败: {e}")
            messagebox.showerror("保存失败", f"❌ 无法写入文件：\n{e}")

    # ============================================================
    # 任务控制逻辑
    # ============================================================
    def set_buttons_state(self, state):
        self.btn_select.config(state=state)
        self.btn_test.config(state=state)
        self.btn_schedule.config(state=state)
        self.ent_trigger.config(state=state)
        self.ent_start.config(state=state)
        self.ent_end.config(state=state)
        if hasattr(self, 'rb_seat_mode_list'):
            self.rb_seat_mode_list.config(state=state)
        if hasattr(self, 'rb_seat_mode_range'):
            self.rb_seat_mode_range.config(state=state)
        if hasattr(self, 'ent_seat_list'):
            self.ent_seat_list.config(state=state)
        if hasattr(self, 'ent_seat_range_start'):
            self.ent_seat_range_start.config(state=state)
        if hasattr(self, 'ent_seat_range_end'):
            self.ent_seat_range_end.config(state=state)
        if hasattr(self, 'btn_save_config'):
            self.btn_save_config.config(state=state)

        if state == tk.NORMAL and hasattr(self, 'seat_mode_var'):
            self._on_seat_mode_change()
        
        # 停止按钮的状态与常规按钮相反
        if state == tk.DISABLED:
            self.btn_stop.config(state=tk.NORMAL)
        else:
            self.btn_stop.config(state=tk.DISABLED)

    def stop_task(self):
        """用户点击停止按钮"""
        if self.running:
            self.task_cancelled = True
            logger.info("🛑 收到停止指令，正在尝试安全退出...")
            self.btn_stop.config(state=tk.DISABLED)

    def _clear_screenshots(self):
        """清理 screenshots 目录下的所有图片文件"""
        folder = "screenshots"
        if not os.path.exists(folder):
            messagebox.showinfo("提示", f"未找到 {folder} 目录。")
            return

        files = [f for f in os.listdir(folder) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        if not files:
            messagebox.showinfo("提示", "截图目录已空，无需清理。")
            return

        if not messagebox.askyesno("确认清理", f"找到 {len(files)} 个截图文件，是否确认全部删除？\n此操作不可撤销。"):
            return

        count = 0
        errors = 0
        for f in files:
            try:
                os.remove(os.path.join(folder, f))
                count += 1
            except Exception as e:
                logger.error(f"删除文件 {f} 失败: {e}")
                errors += 1

        msg = f"✅ 清理完成！已成功删除 {count} 个文件。"
        if errors > 0:
            msg += f"\n注：有 {errors} 个文件删除失败（可能正在被占用）。"
        
        logger.info(msg)
        messagebox.showinfo("清理结果", msg)

    def start_task(self, dry_run: bool, schedule: bool):
        if self.target_hwnd is None:
            messagebox.showerror(
                "未锁定窗口",
                "请先点击【🎯 手动点击锁定窗口】！"
            )
            return
        if self.running:
            return

        self.running = True
        self.task_cancelled = False
        self.set_buttons_state(tk.DISABLED)

        # 解析触发时间
        trigger_time = self.ent_trigger.get().strip()
        try:
            h, m = map(int, trigger_time.split(':'))
        except:
            h, m = 6, 0 # 兜底

        threading.Thread(
            target=self._task_thread, args=(dry_run, schedule, h, m), daemon=True
        ).start()

    def _task_thread(self, dry_run: bool, use_schedule: bool, t_hour: int, t_min: int):
        try:
            if use_schedule:
                self._wait_until_trigger(t_hour, t_min)

            logger.info("=" * 40)
            logger.info(f"开始执行预约流程 [{'DRY_RUN(测试)' if dry_run else '正式提交'}]")

            start_time = self.ent_start.get().strip() or config.BOOKING_START_TIME
            end_time   = self.ent_end.get().strip()   or config.BOOKING_END_TIME
            logger.info(f"预约时间段: {start_time} ~ {end_time}")

            success = booking_flow.run_booking(
                self.target_hwnd,
                dry_run=dry_run,
                start_time=start_time,
                end_time=end_time,
                should_cancel=lambda: self.task_cancelled,
            )

            if success:
                logger.info("✅ 流程正常结束！")
                messagebox.showinfo("成功",
                    "✅ 预约流程顺利完成！\n"
                    "（测试模式请检查日志；正式模式请在小程序中确认结果）"
                )
            else:
                logger.error("❌ 流程未能完成，请查看日志和 screenshots/ 目录排查原因。")
                messagebox.showerror("失败",
                    "❌ 流程终止。\n请查看日志，必要时可编辑「提示词配置」标签页中的提示词。"
                )
        except InterruptedError:
            logger.info("⏹️ 任务已手动停止退出。")
        except booking_flow.BookingError as be:
            logger.warning(f"业务中断: {be}")
            messagebox.showwarning("预约失败", str(be))
        except Exception as e:
            logger.exception("任务异常中断")
            messagebox.showerror("系统异常", f"运行中发生未知错误:\n{e}")
        finally:
            self.running = False
            self.after(0, lambda: self.set_buttons_state(tk.NORMAL))

    def _wait_until_trigger(self, hour, minute):
        """阻塞等待至触发时间"""
        target = datetime.now().replace(
            hour=hour,
            minute=minute,
            second=0, microsecond=0
        )
        if datetime.now() >= target:
            target += timedelta(days=1)

        logger.info(f"⏰ 进入定时模式，等待至 {target.strftime('%H:%M:%S')} ...")

        preheat_target = target - timedelta(seconds=10)
        while True:
            if self.task_cancelled: raise InterruptedError("用户手动停止")
            now = datetime.now()
            if now >= preheat_target:
                break
            time.sleep(1)

        logger.info("🔥 进入倒计时 10 秒预热阶段...")
        while datetime.now() < target:
            if self.task_cancelled: raise InterruptedError("用户手动停止")
            time.sleep(0.01)


def ensure_api_key():
    """启动前检查 API Key，缺失则阻塞式弹窗要求输入并保存至 .env"""
    # 尝试开启高分屏适配（避免对话框模糊）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # 检查是否已有配置
    key = config.API_KEY
    if key and key.strip() and key != "在这里输入您的API密钥":
        return True

    # 初始化一个隐藏的临时 root 用于对话框
    root = tk.Tk()
    root.withdraw()

    new_key = None

    dialog = tk.Toplevel(root)
    dialog.title("初始化配置")
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)
    dialog.transient(root)

    container = ttk.Frame(dialog, padding=16)
    container.pack(fill=tk.BOTH, expand=True)

    ttk.Label(
        container,
        text="检测到未配置阿里云 DashScope API Key。\n视觉识别功能必须有 Key 才能运行。",
        justify=tk.LEFT,
        wraplength=480,
    ).pack(anchor=tk.W, fill=tk.X)

    ttk.Label(container, text="请输入您的 API Key：").pack(anchor=tk.W, pady=(12, 4))

    entry_var = tk.StringVar()
    entry = ttk.Entry(container, textvariable=entry_var, show="*", width=48)
    entry.pack(fill=tk.X)
    entry.focus_set()

    disclaimer_text = (
        "免责声明与提醒：\n"
        "本程序会基于 AI 视觉识别和自动点击执行操作，模型判断并不稳定，结果也无法保证完全正确。\n"
        "运行过程中可能出现误识别、误点击、重复尝试、卡顿或任务失败等情况。\n"
        "如果你不能接受这些可能的风险，请立即退出，不要继续配置或运行。\n"
        "使用时请保持目标窗口在前台，尽量不要手动干预鼠标和键盘，以免影响执行结果。"
    )
    ttk.Label(
        container,
        text=disclaimer_text,
        foreground="#a33",
        justify=tk.LEFT,
        wraplength=480,
    ).pack(anchor=tk.W, fill=tk.X, pady=(12, 0))

    button_row = ttk.Frame(container)
    button_row.pack(fill=tk.X, pady=(16, 0))

    result = {"value": None}

    def _confirm():
        value = entry_var.get().strip()
        if not value:
            messagebox.showwarning("提示", "请输入 API Key 后再继续。", parent=dialog)
            return
        result["value"] = value
        dialog.destroy()

    def _cancel():
        dialog.destroy()

    ttk.Button(button_row, text="取消", command=_cancel).pack(side=tk.RIGHT, padx=(8, 0))
    ttk.Button(button_row, text="确认", command=_confirm).pack(side=tk.RIGHT)

    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.grab_set()
    root.update_idletasks()

    dialog.update_idletasks()
    width = dialog.winfo_reqwidth()
    height = dialog.winfo_reqheight()
    x = (dialog.winfo_screenwidth() - width) // 2
    y = (dialog.winfo_screenheight() - height) // 3
    dialog.geometry(f"+{x}+{y}")

    root.wait_window(dialog)
    new_key = result["value"]

    if not new_key or not new_key.strip():
        messagebox.showwarning("设置取消", "未输入 API Key，程序将退出。")
        root.destroy()
        return False

    # 保存到 .env 文件
    env_path = ".env"
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        
        found = False
        new_lines = []
        key_line = f'API_KEY="{new_key.strip()}"\n'
        for line in lines:
            if line.strip().startswith("API_KEY="):
                new_lines.append(key_line)
                found = True
            else:
                new_lines.append(line)
        
        if not found:
            # 如果文件末尾没有换行，先补一个
            if lines and not lines[-1].endswith('\n'):
                new_lines.append('\n')
            new_lines.append(key_line)
            
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            
        # 更新内存中的配置
        config.API_KEY = new_key.strip()
        messagebox.showinfo("成功", f"API Key 已保存至 {os.path.abspath(env_path)}\n程序即将正常启动。")
        root.destroy()
        return True
    except Exception as e:
        messagebox.showerror("保存失败", f"无法写入配置文件：\n{e}")
        root.destroy()
        return False


if __name__ == "__main__":
    if ensure_api_key():
        app = BookingApp()
        app.mainloop()
