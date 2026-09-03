"""OCR 核心模块测试（不依赖 RapidOCR 真实加载，引擎单例全 mock）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import ocr

# ------------------------------------------------------------------ #
#  输出清洗
# ------------------------------------------------------------------ #


def test_clean_output_strips_and_keeps_inner_lines():
    raw = "\r\n  Hello World  \r\nsecond line\r\n\r\n\n"
    assert ocr.clean_output(raw) == "Hello World\nsecond line"


def test_clean_output_empty():
    assert ocr.clean_output("\n \n\r\n") == ""


def test_clean_output_single_line():
    assert ocr.clean_output("  你好  \r\n") == "你好"


# ------------------------------------------------------------------ #
#  引擎可用性探测
# ------------------------------------------------------------------ #


def test_is_rapidocr_available_true(monkeypatch):
    monkeypatch.setattr(ocr.importlib.util, "find_spec", lambda _name: object())
    assert ocr.is_rapidocr_available() is True


def test_is_rapidocr_available_false_rapidocr_missing(monkeypatch):
    def fake_find_spec(name):
        return None if name == "rapidocr" else object()

    monkeypatch.setattr(ocr.importlib.util, "find_spec", fake_find_spec)
    assert ocr.is_rapidocr_available() is False


def test_is_rapidocr_available_false_onnxruntime_missing(monkeypatch):
    def fake_find_spec(name):
        return None if name == "onnxruntime" else object()

    monkeypatch.setattr(ocr.importlib.util, "find_spec", fake_find_spec)
    assert ocr.is_rapidocr_available() is False


# ------------------------------------------------------------------ #
#  ocr_bytes：mock 引擎单例（避免真实加载模型）
# ------------------------------------------------------------------ #


class _FakeResult:
    def __init__(self, txts, scores=None):
        self.txts = txts
        self.scores = scores


class _FakeEngine:
    """记录调用并返回预设结果。"""

    def __init__(self, result=None, exc=None):
        self.calls: list = []
        self._result = result
        self._exc = exc

    def __call__(self, img):
        self.calls.append(img)
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def fake_engine(monkeypatch):
    """注入假引擎单例，并强制可用性为 True，返回 _FakeEngine 工厂。"""
    monkeypatch.setattr(ocr, "is_rapidocr_available", lambda: True)

    def install(result=None, exc=None) -> _FakeEngine:
        engine = _FakeEngine(result=result, exc=exc)
        monkeypatch.setattr(ocr, "_engine", engine)
        return engine

    return install


@pytest.fixture(scope="module")
def tiny_png():
    """真实极小 PNG：ocr_bytes 现在先 cv2 解码再推理，假字节无法通过。"""
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".png", np.zeros((4, 4, 3), dtype=np.uint8))
    assert ok
    return buf.tobytes()


def test_ocr_bytes_success(fake_engine, tiny_png):
    fake_engine(result=_FakeResult(["离线翻译 Tram", "Hello OCR"]))
    assert ocr.ocr_bytes(tiny_png) == "离线翻译 Tram\nHello OCR"


def test_ocr_bytes_cleans_result(fake_engine, tiny_png):
    fake_engine(result=_FakeResult(["  带尾空格  ", "", ""]))
    assert ocr.ocr_bytes(tiny_png) == "带尾空格"


def test_ocr_bytes_none_result_returns_empty(fake_engine, tiny_png):
    engine = fake_engine(result=None)
    assert ocr.ocr_bytes(tiny_png) == ""
    assert len(engine.calls) == 1


def test_ocr_bytes_empty_txts_returns_empty(fake_engine, tiny_png):
    fake_engine(result=_FakeResult([]))
    assert ocr.ocr_bytes(tiny_png) == ""


def test_ocr_bytes_engine_exception_wrapped(fake_engine, tiny_png):
    fake_engine(exc=RuntimeError("onnx session crashed"))
    with pytest.raises(ocr.OCRError, match="OCR 识别失败"):
        ocr.ocr_bytes(tiny_png)


def test_ocr_bytes_engine_unavailable(monkeypatch):
    monkeypatch.setattr(ocr, "is_rapidocr_available", lambda: False)
    with pytest.raises(ocr.OCRError, match="未安装"):
        ocr.ocr_bytes(b"png")  # 校验先于解码：假字节也能验证错误优先级


def test_ocr_bytes_unsupported_language(fake_engine):
    engine = fake_engine(result=_FakeResult(["x"]))
    with pytest.raises(ocr.OCRError, match="不支持"):
        ocr.ocr_bytes(b"png", "kor")  # 校验先于解码与引擎调用
    assert engine.calls == []


# ------------------------------------------------------------------ #
#  语言别名映射（旧 Tesseract 配置兼容）
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "lang",
    ["chi_sim+eng", "chi_tra+eng", "eng", "CH",
    "jpn", "japan", "ja", "JPN",
])
def test_ocr_bytes_accepts_various_language_codes(fake_engine, tiny_png, lang):
    fake_engine(result=_FakeResult(["ok"]))
    assert ocr.ocr_bytes(tiny_png, lang) == "ok"


# ------------------------------------------------------------------ #
#  ndarray 路径（区域监控复用：png_to_ndarray / ocr_lines）
# ------------------------------------------------------------------ #


def test_png_to_ndarray_decodes(tiny_png):
    img = ocr.png_to_ndarray(tiny_png)
    assert img.shape == (4, 4, 3)  # BGR 三通道


def test_png_to_ndarray_invalid_raises():
    with pytest.raises(ocr.OCRError, match="解码失败"):
        ocr.png_to_ndarray(b"png")


def test_ocr_lines_returns_scores(fake_engine, tiny_png):
    fake_engine(
        result=_FakeResult(["你好", "world"], scores=[0.95, 0.72])
    )
    assert ocr.ocr_lines(ocr.png_to_ndarray(tiny_png)) == [
        ("你好", 0.95),
        ("world", 0.72),
    ]


def test_ocr_lines_missing_scores_fallback(fake_engine, tiny_png):
    """引擎未返回 scores 时置信度回退 0.0（不应崩溃）。"""
    fake_engine(result=_FakeResult(["abc"], scores=None))
    assert ocr.ocr_lines(ocr.png_to_ndarray(tiny_png)) == [("abc", 0.0)]


# ------------------------------------------------------------------ #
#  预处理（pixmap_to_png 需要 QApplication，离屏即可）
# ------------------------------------------------------------------ #


def test_pixmap_to_png_upscales_small_region(qapp):
    from PyQt6.QtGui import QPixmap

    pm = QPixmap(100, 30)
    pm.fill()
    png = ocr.pixmap_to_png(pm)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # 高度 < 60 应放大 2 倍（100x30 -> 200x60）
    from PyQt6.QtGui import QImage

    img = QImage.fromData(png)
    assert img.height() == 60
    assert img.width() == 200


@pytest.fixture
def qapp():
    """离屏 QApplication（无显示环境也可运行 GUI 相关单测）。"""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


# ------------------------------------------------------------------ #
#  集成：真实引擎端到端往返（未装 ocr extras 的环境自动跳过）
# ------------------------------------------------------------------ #

# 夹具为真实桌面会话渲染的清晰文字图（offscreen QPA 无字体无法生成）
_OCR_FIXTURE = Path(__file__).parent / "data" / "ocr_fixture.png"


@pytest.mark.skipif(
    not ocr.is_rapidocr_available() or not _OCR_FIXTURE.is_file(),
    reason="未安装 rapidocr/onnxruntime 或缺少夹具",
)
def test_real_ocr_roundtrip():
    """已知文字图像 → ocr_bytes → 断言识别出中英文关键词。"""
    text = ocr.ocr_bytes(_OCR_FIXTURE.read_bytes(), "ch")
    assert "Tram" in text, f"OCR 结果: {text!r}"
    assert "Hello" in text, f"OCR 结果: {text!r}"
    assert "离线" in text, f"OCR 结果: {text!r}"


@pytest.mark.skipif(
    not ocr.is_rapidocr_available(),
    reason="未安装 rapidocr/onnxruntime",
)
def test_real_ocr_japanese(tmp_path):
    """日语识别端到端验证：动态生成日语测试图 → ocr_bytes → 断言识别正确。"""
    from PIL import Image, ImageDraw, ImageFont

    # 查找可用日语字体（Windows 内置）
    font_paths = [
        r"C:\Windows\Fonts\msgothic.ttc",
        r"C:\Windows\Fonts\meiryo.ttc",
        r"C:\Windows\Fonts\msmincho.ttc",
        r"C:\Windows\Fonts\yugothic.ttc",
    ]
    font = None
    for fp in font_paths:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 36)
                break
            except Exception:
                continue
    if font is None:
        pytest.skip("系统中未找到可渲染日语的字体")

    # 生成清晰的日语测试图
    text = "吾輩は猫である"
    bbox = font.getbbox(text)
    w, h = bbox[2] + 40, bbox[3] + 30
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 10), text, font=font, fill="black")

    png_path = tmp_path / "jpn_test.png"
    img.save(png_path)

    result = ocr.ocr_bytes(png_path.read_bytes(), "jpn")
    # 关键字应能被识别（允许少量误差，但核心词必须命中）
    assert "猫" in result or "吾輩" in result or "である" in result, (
        f"日语 OCR 结果: {result!r}"
    )
