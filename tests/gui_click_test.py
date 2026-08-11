"""GUI 点击换算测试：居中偏移后坐标精确、空白区域不点击、无图不点击。

不创建真实 Tk 窗口：__new__ 构造 GuiApp，mock 图片/photo 与网络提交。
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import gui as gui_mod  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


class FakePhoto:
    def __init__(self, w, h):
        self._w, self._h = w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


class FakeEvent:
    def __init__(self, x, y):
        self.x, self.y = x, y


def make_app(img_w=1920, img_h=1080, dw=960, dh=540, offx=10, offy=20):
    """构造 GuiApp：原始图 1920x1080，显示 960x540，居中偏移 (10,20)。"""
    app = gui_mod.GuiApp.__new__(gui_mod.GuiApp)
    app._img = mock.Mock()
    app._img.width = img_w
    app._img.height = img_h
    app._photo = FakePhoto(dw, dh)
    app._img_offx = offx
    app._img_offy = offy
    app.dbl_var = mock.Mock()
    app.dbl_var.get = mock.Mock(return_value=False)
    app._toast = mock.Mock()
    app._submit_execute = mock.Mock()
    return app


print("== 1. 区域内点击精确换算 ==")
app = make_app()
# 图片左上角 (10,20)，显示尺寸 960x540；点击图片中心 → 屏幕坐标 (960, 540)
app._on_canvas_click(FakeEvent(10 + 480, 20 + 270))
app._submit_execute.assert_called_once()
payload = app._submit_execute.call_args.args[0]
check("中心点击换算", payload["action"] == "click" and payload["x"] == 960 and payload["y"] == 540,
      str(payload))

# 点击图片左上角像素 → (0, 0)
app._submit_execute.reset_mock()
app._on_canvas_click(FakeEvent(10, 20))
payload = app._submit_execute.call_args.args[0]
check("左上角换算", payload["x"] == 0 and payload["y"] == 0, str(payload))

# 点击图片右下显示像素 → 映射到屏幕右下区间起点 (1918, 1078)
# （floor 映射：dw 个显示像素覆盖 img_w 个屏幕像素，末像素映射 1918）
app._submit_execute.reset_mock()
app._on_canvas_click(FakeEvent(10 + 959, 20 + 539))
payload = app._submit_execute.call_args.args[0]
check("右下角换算", payload["x"] == 1918 and payload["y"] == 1078, str(payload))

print("== 2. 空白区域不点击 ==")
app = make_app()
# 图片右侧空白（x > offx+dw）
app._on_canvas_click(FakeEvent(10 + 960 + 50, 20 + 100))
check("右侧空白不点击", not app._submit_execute.called, "")
# 图片下方空白
app._on_canvas_click(FakeEvent(10 + 100, 20 + 540 + 50))
check("下方空白不点击", not app._submit_execute.called, "")
# 左上空白（偏移外）
app._on_canvas_click(FakeEvent(5, 5))
check("左上空白不点击", not app._submit_execute.called, "")

print("== 3. 无图 / 无显示不点击 ==")
app2 = make_app()
app2._img = None
app2._on_canvas_click(FakeEvent(100, 100))
check("无图不点击", not app2._submit_execute.called, "")

app3 = make_app()
app3._photo = FakePhoto(0, 0)
app3._on_canvas_click(FakeEvent(100, 100))
check("零尺寸不点击", not app3._submit_execute.called, "")

print("== 4. 双击选项传递 ==")
app4 = make_app()
app4.dbl_var.get = mock.Mock(return_value=True)
app4._on_canvas_click(FakeEvent(10 + 480, 20 + 270))
payload = app4._submit_execute.call_args.args[0]
check("双击 clicks=2", payload["clicks"] == 2, str(payload))

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
