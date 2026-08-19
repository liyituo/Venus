"""Windows 分层窗口：把 RGBA 图贴到无边框窗口上，窗外真正透明。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
HTCLIENT = 1
DIB_RGB_COLORS = 0
BI_RGB = 0
GA_ROOT = 2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]


user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.restype = ctypes.c_long
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), ctypes.c_uint,
]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, ctypes.c_uint,
]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]

user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.CallWindowProcW.restype = ctypes.c_long
user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ScreenToClient.restype = wintypes.BOOL

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_hit_alpha: bytes | None = None
_hit_w = 0
_hit_h = 0
_orig_wndproc: int | None = None
_wndproc_ref = None


def _alpha_at(x: int, y: int) -> int:
    if _hit_alpha is None or x < 0 or y < 0 or x >= _hit_w or y >= _hit_h:
        return 0
    return _hit_alpha[y * _hit_w + x]


def set_hit_mask(image: Image.Image) -> None:
    """保存 alpha 通道，供 WM_NCHITTEST 判断透明区域是否穿透点击。"""
    global _hit_alpha, _hit_w, _hit_h
    rgba = image.convert("RGBA")
    _hit_w, _hit_h = rgba.size
    _hit_alpha = rgba.getchannel("A").tobytes()


def install_click_through(hwnd: int) -> None:
    """透明像素返回 HTTRANSPARENT，不再挡住桌面/其它窗口的鼠标点击。"""
    global _orig_wndproc, _wndproc_ref
    if _orig_wndproc is not None:
        return

    @WNDPROC
    def wndproc(h, msg, wparam, lparam):
        if msg == WM_NCHITTEST and _hit_alpha:
            sx = ctypes.c_short(lparam & 0xFFFF).value
            sy = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            pt = POINT(sx, sy)
            user32.ScreenToClient(h, ctypes.byref(pt))
            if _alpha_at(pt.x, pt.y) > 8:
                return HTCLIENT
            return HTTRANSPARENT
        return user32.CallWindowProcW(_orig_wndproc, h, msg, wparam, lparam)

    _wndproc_ref = wndproc
    handle = wintypes.HWND(hwnd)
    _orig_wndproc = user32.SetWindowLongPtrW(handle, GWLP_WNDPROC, _wndproc_ref)
    if not _orig_wndproc:
        raise OSError(f"SetWindowLongPtrW failed ({ctypes.GetLastError()})")


def hwnd_of(root) -> int:
    root.update_idletasks()
    try:
        frame = int(str(root.wm_frame()), 16)
        if frame:
            return frame
    except (TypeError, ValueError):
        pass
    child = wintypes.HWND(int(root.winfo_id()))
    ancestor = user32.GetAncestor(child, GA_ROOT)
    return int(ancestor or child)


def enable_layered(hwnd: int) -> None:
    handle = wintypes.HWND(hwnd)
    style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
    style = (style | WS_EX_LAYERED | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    user32.SetWindowLongW(handle, GWL_EXSTYLE, style)
    user32.SetWindowPos(
        handle, 0, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
    )


def _premultiply_bgra(image: Image.Image) -> bytes:
    flipped = image.convert("RGBA").transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    src = flipped.tobytes()
    out = bytearray(len(src))
    for i in range(0, len(src), 4):
        a = src[i + 3]
        out[i] = src[i + 2] * a // 255
        out[i + 1] = src[i + 1] * a // 255
        out[i + 2] = src[i] * a // 255
        out[i + 3] = a
    return bytes(out)


def present(hwnd: int, image: Image.Image) -> None:
    width, height = image.size
    bgra = _premultiply_bgra(image)
    handle = wintypes.HWND(hwnd)
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    bits = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(
        hdc_mem, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0,
    )
    if not dib or not bits:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        raise OSError("CreateDIBSection failed")
    ctypes.memmove(bits, bgra, len(bgra))
    old = gdi32.SelectObject(hdc_mem, dib)
    blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
    size = SIZE(width, height)
    src = POINT(0, 0)
    # pptDst 必须为 NULL，否则会把窗口拖到错误坐标，画面直接消失
    ok = user32.UpdateLayeredWindow(
        handle, hdc_screen, None, ctypes.byref(size),
        hdc_mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA,
    )
    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(dib)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    if not ok:
        raise OSError(f"UpdateLayeredWindow failed ({ctypes.GetLastError()})")
