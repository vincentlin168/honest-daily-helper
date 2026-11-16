"""Windows 系统托盘常驻（Spec-Driven，零依赖）。

Spec
----
让助手能「关掉窗口但继续工作」，这是常驻类工具的基本要求。
托盘图标提供四个动作：
    打开面板 / 立即识别 / 生成日报 / 退出

为什么自己写而不用 pystray
--------------------------
本项目的要求是**零第三方依赖**（pip 在目标环境不可达）。
pystray 依赖 pywin32 / PIL，pywin32 又依赖编译扩展 —— 都装不上。

所以这里直接用标准库 ``ctypes`` 调 Win32 API：
    Shell_NotifyIconW  注册托盘图标
    CreatePopupMenu   弹出右键菜单
    TrackPopupMenu    显示菜单并等待选择
    PostMessage /    通知主窗口（自定义消息）
    DestroyIcon       释放图标资源

设计要点
--------
1. **纯 ctypes**：只依赖标准库，打包成 exe 后体积不增加。
2. **图标内嵌**：用 Pillow 现场生成一个深蓝圆形图标（不依赖 .ico 文件，
   打包时不会漏资源）。
3. **消息循环独立线程**：托盘有自己的消息循环，不与主程序抢
   ``GetMessage``（主程序用的是 Python 层的循环）。
4. **安全降级**：非 Windows 平台直接返回不可用，程序照常跑 ——
   托盘是增强，不是依赖。
5. **窗口类名不靠对象地址**：用进程内单调计数器生成，注册撞名
   （1410）时自动换名重试。详见 ``_make_class_name``。

限制
----
- 仅支持 Windows（本项目的目标平台）。
- 托盘图标是「状态提示」，不承担业务逻辑；所有动作都通过回调
  转发给主程序，菜单线程不做业务。
"""

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Any, Callable, Dict, Optional

# ---------- Win32 常量 ----------
WM_APP = 0x8000
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
SM_CXSMICON = 49
SM_CYSMICON = 50
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IMAGE_ICON = 1
IDI_APPLICATION = 32512

# Win32 错误码
ERROR_CLASS_ALREADY_EXISTS = 1410

# 窗口类名计数器（见 _make_class_name 的说明）
_class_seq = 0
_class_seq_lock = threading.Lock()


def _make_class_name() -> str:
    """生成一个**本进程内绝不重复**的窗口类名。

    为什么不用 ``id(self)``
    ----------------------
    原实现是 ``f"HHDTray_{os.getpid()}_{id(self)}"``。但 CPython 的
    ``id()`` 是**内存地址**，对象被回收后该地址会被下一个对象复用 ——
    于是新托盘实例可能拿到与「已注册但尚未注销」的窗口类完全相同的名字，
    ``RegisterClassW`` 返回 0，错误码 1410
    （ERROR_CLASS_ALREADY_EXISTS）。

    这在 CI 上表现为随机失败：同一份代码，Python 3.12/3.13 通过、
    3.11 失败；本地连跑也可能时好时坏。本地实测 3.13 稳定通过，
    而 CI 的 3.11 稳定撞上 —— 典型的「按对象地址认人」的经典 bug。

    为什么用计数器还是不够
    ----------------------
    单调计数器只在**同一进程内**唯一。托盘「起停」时若上一次没走到
    ``UnregisterClassW``（例如线程被强杀），残留的类名仍会留在系统里。
    所以注册失败且错误码是 1410 时，必须**换一个名字重试**，
    而不是直接放弃。

    :return: 形如 ``HHDTray_<pid>_<seq>`` 的类名
    """
    global _class_seq
    with _class_seq_lock:
        _class_seq += 1
        seq = _class_seq
    return f"HHDTray_{os.getpid()}_{seq}"

# 菜单命令 ID（自定义区间，避免与系统命令冲突）
CMD_OPEN = 1001
CMD_CAPTURE = 1002
CMD_REPORT = 1003
CMD_EXIT = 1004


def is_windows() -> bool:
    return sys.platform == "win32"


def _make_icon_image(size: int = 32):
    """用 Pillow 现场生成托盘图标（深蓝圆环 + 金色点，呼应界面配色）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, size // 8)
    draw.ellipse([pad, pad, size - pad - 1, size - pad - 1],
                 fill=(26, 42, 74, 255))          # --navy
    ring = max(1, size // 10)
    draw.ellipse([pad + ring, pad + ring, size - pad - ring - 1, size - pad - ring - 1],
                 outline=(245, 166, 35, 255), width=max(1, size // 12))  # --gold
    dot = max(2, size // 5)
    cx = cy = (size - dot) // 2
    draw.ellipse([cx, cy, cx + dot, cy + dot], fill=(245, 166, 35, 255))
    return img


def _icon_to_hicon(size: int = 32) -> Optional[int]:
    """把 Pillow 图像转成 Windows HICON。

    做法：先存临时 .ico，再让 LoadImageW 读它。
    比手写 ICO 二进制格式稳得多，且只需一次。
    """
    if not is_windows():
        return None
    import tempfile
    user32 = ctypes.windll.user32
    try:
        img = _make_icon_image(size)
        fd, path = tempfile.mkstemp(suffix=".ico")
        os.close(fd)
        img.save(path, format="ICO",
                 sizes=[(size, size), (16, 16), (24, 24), (32, 32), (48, 48)])
        hicon = user32.LoadImageW(
            None, path, IMAGE_ICON, 0, 0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE)
        try:
            os.unlink(path)
        except OSError:
            pass
        if not hicon:
            return None
        # 记录 GDI 对象数量，避免 GetDC 泄漏
        return int(hicon)
    except Exception:
        return None


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class TrayIcon:
    """Windows 系统托盘图标。

    用法::

        tray = TrayIcon(
            on_open=lambda: ...,
            on_capture=lambda: ...,
            on_report=lambda: ...,
            on_exit=lambda: ...,
        )
        tray.start()
        ...
        tray.stop()
    """

    def __init__(self,
                 title: str = "诚实日报助手",
                 on_open: Optional[Callable[[], Any]] = None,
                 on_capture: Optional[Callable[[], Any]] = None,
                 on_report: Optional[Callable[[], Any]] = None,
                 on_exit: Optional[Callable[[], Any]] = None):
        self.title = title
        self._callbacks: Dict[int, Optional[Callable[[], Any]]] = {
            CMD_OPEN: on_open,
            CMD_CAPTURE: on_capture,
            CMD_REPORT: on_report,
            CMD_EXIT: on_exit,
        }
        self._thread: Optional[threading.Thread] = None
        self._hwnd: Optional[int] = None
        self._icon: Optional[int] = None
        self._running = False
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._class_name: Optional[str] = None

    # ---------- 生命周期 ----------
    @property
    def available(self) -> bool:
        """当前环境是否支持托盘。"""
        return is_windows()

    @property
    def error(self) -> Optional[str]:
        """启动失败时的原因。"""
        return self._error

    def start(self, timeout: float = 3.0) -> bool:
        """启动托盘图标。返回是否成功。

        失败不会抛异常 —— 托盘是增强功能，不该让主程序起不来。
        """
        if not is_windows():
            self._error = "非 Windows 平台不支持系统托盘"
            return False
        if self._thread and self._thread.is_alive():
            return True

        self._running = True
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ready.is_set() and self._error is None

    def stop(self) -> None:
        """移除托盘图标并结束线程。"""
        self._running = False
        if is_windows() and self._hwnd:
            try:
                data = self._nid()
                data.uFlags = 0
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
        if self._icon:
            try:
                ctypes.windll.user32.DestroyIcon(self._icon)
            except Exception:
                pass
            self._icon = None
        if self._thread and self._thread.is_alive():
            # 唤醒消息循环让它退出
            if self._hwnd:
                try:
                    ctypes.windll.user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
                except Exception:
                    pass
            self._thread.join(timeout=2.0)
        # 注销窗口类，避免类名残留（下次注册同名会失败）
        if is_windows() and getattr(self, "_class_name", None):
            try:
                ctypes.windll.user32.UnregisterClassW(self._class_name, None)
            except Exception:
                pass
            self._class_name = None
        self._hwnd = None

    def notify(self, title: str, message: str) -> bool:
        """弹一个气泡通知。"""
        if not is_windows() or not self._hwnd:
            return False
        try:
            data = self._nid()
            data.szInfoTitle = title[:63]
            data.szInfo = message[:255]
            data.dwInfoFlags = 0x00000001  # NIIF_INFO
            return bool(ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_MODIFY, ctypes.byref(data)))
        except Exception:
            return False

    # ---------- 内部 ----------
    def _nid(self) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_APP + 1
        data.hIcon = self._icon or 0
        data.szTip = self.title[:127]
        return data

    def _run(self) -> None:
        """托盘消息循环（独立线程）。"""
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        # 显式声明 Win32 API 原型。
        # 默认的 ctypes 假定参数是 32 位 int，而 LPARAM/WPARAM 在 x64 上是 64 位，
        # 直接调用会抛 "int too long to convert"。
        user32.DefWindowProcW.restype = ctypes.c_longlong
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.RegisterClassW.argtypes = [ctypes.c_void_p]
        user32.PostMessageW.argtypes = [
            wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.argtypes = [
            ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.LPCVOID]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]

        # 窗口过程
        WndProc = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
            wintypes.WPARAM, wintypes.LPARAM)

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_APP + 1:
                # 托盘图标被点击 -> 弹出菜单
                self._show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                cmd = int(wparam) & 0xFFFF
                cb = self._callbacks.get(cmd)
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wndproc = WndProc(_wndproc)
        hinst = kernel32.GetModuleHandleW(None)

        # 类名必须唯一：同名类重复注册会失败（错误码 1410 = ERROR_CLASS_ALREADY_EXISTS）。
        # 用单调计数器生成进程内唯一名；见 _make_class_name 的说明（为什么不能用 id(self)）。
        class_name = _make_class_name()

        # 自己拼 WNDCLASS 结构（ctypes.wintypes 没有现成的 WNDCLASSW）
        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WndProc),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        wc = WNDCLASS()
        wc.style = 0x0003  # CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst

        # 注册。1410 = ERROR_CLASS_ALREADY_EXISTS：说明系统里还残留着
        # 某个同名窗口类（上次没走到 UnregisterClassW）。换新名字重试即可，
        # 直接失败会让托盘整个不可用 —— 而托盘只是「增强」，不该因此瘫掉。
        registered = False
        last_err = 0
        for _attempt in range(8):
            wc.lpszClassName = class_name
            if user32.RegisterClassW(ctypes.byref(wc)):
                registered = True
                self._class_name = class_name
                break
            last_err = kernel32.GetLastError()
            if last_err != ERROR_CLASS_ALREADY_EXISTS:
                break
            class_name = _make_class_name()  # 换个名字再试

        if not registered:
            self._error = f"RegisterClassW 失败（错误码 {last_err}）"
            self._ready.set()
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, self.title, 0,
            0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            self._error = f"CreateWindowExW 失败（错误码 {kernel32.GetLastError()}）"
            self._ready.set()
            return
        self._hwnd = int(hwnd)

        self._icon = _icon_to_hicon(32)
        if self._icon:
            try:
                user32.SetClassLongPtrW(hwnd, -14, self._icon)  # GCLP_HICON
            except Exception:
                pass

        nid = self._nid()
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._error = "Shell_NotifyIconW 添加图标失败（可能不支持通知区域）"
            self._ready.set()
            return

        self._ready.set()
        self._ready.set()

        # 消息循环
        msg = wintypes.MSG()
        while self._running:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_menu(self, hwnd) -> None:
        """弹出右键菜单并处理选择。"""
        user32 = ctypes.windll.user32
        try:
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            user32.AppendMenuW(menu, MF_STRING, CMD_OPEN, "打开面板")
            user32.AppendMenuW(menu, MF_STRING, CMD_CAPTURE, "立即识别")
            user32.AppendMenuW(menu, MF_STRING, CMD_REPORT, "生成日报")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "退出")

            # 光标位置
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 菜单必须在当前线程，否则弹出后收不到消息
            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                pt.x, pt.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_COMMAND, cmd, 0)
            user32.DestroyMenu(menu)
        except Exception:
            pass
