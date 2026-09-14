"""Win32 API 封装（ctypes）。

只声明本程序用得到的部分。目标是彻底绕开 Qt / Pillow 这类大依赖——
托盘用 Shell_NotifyIcon，窗口用 CreateWindowEx + GDI 自绘，
实测常驻内存只有 Qt 方案的约四分之一。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)

# --------------------------------------------------------------------- 常量

# 窗口样式
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZE = 0x01000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
CW_USEDEFAULT = -2147483648

# GetWindowLong 偏移
GWL_STYLE = -16

# 显示
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)

# 消息
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
WM_ERASEBKGND = 0x0014
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSEMOVE = 0x0200
WM_SETCURSOR = 0x0020
WM_KEYDOWN = 0x0100
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_LBUTTONDBLCLK = 0x0203
WM_PIN_READY = WM_APP + 2  # 自用：常驻任务的准备结果通知主线程
WM_TRAY_READDED = WM_APP + 3  # 自用：explorer 重启后托盘已重新注册

# MessageBox
MB_OK = 0x00000000
MB_YESNO = 0x00000004
MB_ICONQUESTION = 0x00000020
MB_ICONINFORMATION = 0x00000040
MB_ICONWARNING = 0x00000030
IDYES = 6

# 其它
ERROR_ALREADY_EXISTS = 183

# 托盘
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NOTIFYICON_VERSION_4 = 4

# 菜单
MF_STRING = 0x00000000
MF_POPUP = 0x00000010
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_UNCHECKED = 0x00000000
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

# GDI
TRANSPARENT = 1
DIB_RGB_COLORS = 0
BI_RGB = 0
DT_LEFT = 0x0000
DT_CENTER = 0x0001
DT_RIGHT = 0x0002
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800
DT_WORDBREAK = 0x0010
PS_SOLID = 0
NULL_BRUSH = 5
NULL_PEN = 8
DEFAULT_CHARSET = 1
FW_NORMAL = 400
FW_SEMIBOLD = 600
FW_BOLD = 700
CLEARTYPE_QUALITY = 5
ANTIALIASED_QUALITY = 4
DEFAULT_GUI_FONT = 17

# Shell_NotifyIcon 之外还会用到的
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_CXSMICON = 49
SM_CYSMICON = 50
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
IDC_ARROW = 32512

# --------------------------------------------------------------------- 结构


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
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
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


# --------------------------------------------------------------------- 函数

# --- user32 ---
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.WORD

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.DefWindowProcW.argtypes = [
    wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
]
user32.DefWindowProcW.restype = LRESULT

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT

user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT

user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UpdateWindow.argtypes = [wintypes.HWND]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
user32.SetCapture.argtypes = [wintypes.HWND]
user32.ReleaseCapture.argtypes = []
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.AdjustWindowRectEx.argtypes = [
    ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
]
user32.AdjustWindowRectEx.restype = wintypes.BOOL
user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT
]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL

# --- gdi32 ---
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPCWSTR,
]
gdi32.CreateFontW.restype = wintypes.HGDIOBJ
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.CreatePen.restype = wintypes.HPEN
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetTextColor.restype = wintypes.DWORD
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
# DrawTextW 在 user32（虽然做的是 GDI 文本输出）
user32.DrawTextW.argtypes = [
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT
]
user32.DrawTextW.restype = ctypes.c_int
gdi32.RoundRect.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int,
]
gdi32.Rectangle.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.MoveToEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.LineTo.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.Polyline.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.c_int]
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wintypes.HGDIOBJ
gdi32.GetTextExtentPoint32W.argtypes = [
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(SIZE)
]
gdi32.Polygon.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.c_int]
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int
gdi32.CreateRoundRectRgn.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
]
gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
gdi32.SelectClipRgn.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectClipRgn.restype = ctypes.c_int

# --- shell32 ---
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

# --- kernel32 ---
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetTickCount64.argtypes = []
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
kernel32.QueryUnbiasedInterruptTime.restype = wintypes.BOOL


# ----------------------------------------------------------- 原生控件（对话框）
# 「电价设置」窗口用系统自带控件（STATIC / EDIT / BUTTON / COMBOBOX）搭。
# 自己画虽然可控，但光标、选区、输入法、Tab 轮转、右键菜单这些都要重做，
# 交给系统实现性价比高得多。

# 子窗口 / 扩展样式
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_BORDER = 0x00800000
WS_TABSTOP = 0x00010000
WS_GROUP = 0x00020000
WS_VSCROLL = 0x00200000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_CONTROLPARENT = 0x00010000

# 编辑框（EDIT）
ES_LEFT = 0x0000
ES_RIGHT = 0x0002
ES_MULTILINE = 0x0004
ES_AUTOHSCROLL = 0x0080
EM_SETSEL = 0x00B1
EM_SETLIMITTEXT = 0x00C5

# 下拉框（COMBOBOX）
CBS_DROPDOWNLIST = 0x0003
CBS_AUTOHSCROLL = 0x0040
CBS_HASSTRINGS = 0x0200
CBS_NOINTEGRALHEIGHT = 0x0400
CB_ADDSTRING = 0x0143
CB_GETCURSEL = 0x0147
CB_GETLBTEXT = 0x0148
CB_GETLBTEXTLEN = 0x0149
CB_RESETCONTENT = 0x014B
CB_SETCURSEL = 0x014E
CB_SETITEMHEIGHT = 0x0153

# 按钮 / 静态文本
BS_PUSHBUTTON = 0x00000000
BS_DEFPUSHBUTTON = 0x00000001
SS_LEFT = 0x00000000
SS_RIGHT = 0x00000002
SS_LEFTNOWORDWRAP = 0x0000000C
SS_NOPREFIX = 0x00000080

# 控件相关消息
WM_SETFONT = 0x0030
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_CTLCOLOREDIT = 0x0133
WM_CTLCOLORLISTBOX = 0x0134
WM_CTLCOLORBTN = 0x0135
WM_CTLCOLORSTATIC = 0x0138
DM_GETDEFID = 0x0400
DC_HASDEFID = 0x534B
IDOK = 1
IDCANCEL = 2
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

# WM_COMMAND 高位字里的通知码
BN_CLICKED = 0
CBN_SELCHANGE = 1
EN_CHANGE = 0x0300

# 系统颜色
COLOR_WINDOW = 5
COLOR_HIGHLIGHT = 13
COLOR_BTNFACE = 15
COLOR_GRAYTEXT = 17

MB_ICONERROR = 0x00000010

# --- user32：控件相关 ---
user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetDlgItem.restype = wintypes.HWND
user32.SendDlgItemMessageW.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
]
user32.SendDlgItemMessageW.restype = LRESULT
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.SetWindowTextW.restype = wintypes.BOOL
user32.IsDialogMessageW.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.MSG)]
user32.IsDialogMessageW.restype = wintypes.BOOL
user32.SetFocus.argtypes = [wintypes.HWND]
user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.EnableWindow.restype = wintypes.BOOL
user32.MoveWindow.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL
]
user32.MoveWindow.restype = wintypes.BOOL
user32.GetSysColorBrush.argtypes = [ctypes.c_int]
user32.GetSysColorBrush.restype = wintypes.HBRUSH
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

# --- gdi32 补充 ---
gdi32.SetBkColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetBkColor.restype = wintypes.DWORD
# 字距微调：小号标签拉开一点会显得透气（中文不拉）
gdi32.SetTextCharacterExtra.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetTextCharacterExtra.restype = ctypes.c_int
# 多边形裁剪区：曲线下方的渐变面积要按折线形状裁
gdi32.CreatePolygonRgn.argtypes = [
    ctypes.POINTER(wintypes.POINT), ctypes.c_int, ctypes.c_int
]
gdi32.CreatePolygonRgn.restype = wintypes.HANDLE
WINDING = 2
# 取屏幕颜色：任务栏长条要采一下任务栏底色，才能跟系统主题融在一起
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD

# ------------------------------------------------------- 分层窗口（真圆角 + 真柔影）
# 普通窗口画不出圆角，内存 DC 里也没有 alpha（BitBlt 会把 alpha 直接丢掉）。
# 想要「圆角能抗锯齿、阴影能渐变」，只有 UpdateLayeredWindow 一条路：
# 自己准备一块 32bpp 顶朝下 DIB，在 Python 里逐像素写好**预乘** alpha，
# 再整块贴到窗口上。代价是分层窗口不走 WM_PAINT，每次内容变了直接重贴。

WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
# 鼠标事件直接穿过去打到下面的窗口（长条就盖在任务栏上，不能把任务栏的
# 右键菜单/悬停提示吃掉）
WS_EX_TRANSPARENT = 0x00000020
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SWP_NOOWNERZORDER = 0x0200


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
