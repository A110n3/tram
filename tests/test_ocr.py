"""OCR 核心模块测试（不依赖 Tesseract 实际安装，subprocess 全 mock）。"""

from __future__ import annotations

import subprocess
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
#  参数构造
# ------------------------------------------------------------------ #


def test_build_args_shape(tmp_path):
    exe = tmp_path / "tesseract.exe"
    png = tmp_path / "shot.png"
    args = ocr._build_args(exe, png, "chi_sim+eng")
    assert args[0] == str(exe)
    assert args[1] == str(png)
    assert args[2] == "stdout"
    # -l 与语言参数成对出现
    i = args.index("-l")
    assert args[i + 1] == "chi_sim+eng"
    # --psm 3（全自动分割）
    j = args.index("--psm")
    assert args[j + 1] == "3"


# ------------------------------------------------------------------ #
#  二进制定位逻辑
# ------------------------------------------------------------------ #


def _isolate_discovery(monkeypatch, tmp_path, vendor_dir=None, which=None,
                       program_files=None):
    """屏蔽所有真实环境，按用例注入候选路径。"""
    dirs = [vendor_dir] if vendor_dir is not None else []
    monkeypatch.setattr(ocr, "_vendor_dirs", lambda: dirs)
    monkeypatch.setattr(ocr.shutil, "which", lambda _: which)
    monkeypatch.setattr(ocr.os.environ, "get", lambda k, d="": {
        "PROGRAMFILES": program_files or "",
        "PROGRAMFILES(X86)": "",
    }.get(k, d))


def test_find_tesseract_in_vendor(monkeypatch, tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    exe = vendor / "tesseract.exe"
    exe.write_bytes(b"fake")
    _isolate_discovery(monkeypatch, tmp_path, vendor_dir=vendor)
    assert ocr.find_tesseract() == exe


def test_find_tesseract_in_program_files(monkeypatch, tmp_path):
    pf = tmp_path / "pf"
    (pf / "Tesseract-OCR").mkdir(parents=True)
    exe = pf / "Tesseract-OCR" / "tesseract.exe"
    exe.write_bytes(b"fake")
    _isolate_discovery(monkeypatch, tmp_path, program_files=str(pf))
    assert ocr.find_tesseract() == exe


def test_find_tesseract_not_found(monkeypatch, tmp_path):
    _isolate_discovery(monkeypatch, tmp_path)
    assert ocr.find_tesseract() is None


# ------------------------------------------------------------------ #
#  ocr_bytes：mock subprocess
# ------------------------------------------------------------------ #


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_tesseract(monkeypatch, tmp_path):
    """注入 vendor 目录中的假 tesseract.exe，返回可配置的 run 记录器。"""
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    exe = vendor / "tesseract.exe"
    exe.write_bytes(b"fake")
    calls: dict = {}

    def fake_run(args, capture_output=True, timeout=None, env=None):
        calls["args"] = args
        calls["timeout"] = timeout
        calls["env"] = env
        return _FakeProc(stdout=calls.get("stdout", "识别结果\n\n".encode()))

    monkeypatch.setattr(ocr, "_vendor_dirs", lambda: [vendor])
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    return calls


def test_ocr_bytes_success(fake_tesseract):
    fake_tesseract["stdout"] = b"Hello\nWorld\n\n"
    assert ocr.ocr_bytes(b"png", "eng") == "Hello\nWorld"
    assert fake_tesseract["timeout"] == ocr.OCR_TIMEOUT_S


def test_ocr_bytes_sets_tessdata_prefix(fake_tesseract, tmp_path):
    ocr.ocr_bytes(b"png")
    env = fake_tesseract["env"]
    assert "TESSDATA_PREFIX" not in env  # 无 tessdata 目录时不设置


def test_ocr_bytes_missing_binary(monkeypatch):
    monkeypatch.setattr(ocr, "_vendor_dirs", lambda: [])
    monkeypatch.setattr(ocr.shutil, "which", lambda _: None)
    monkeypatch.setattr(ocr.os.environ, "get", lambda k, d="": "")
    with pytest.raises(ocr.OCRError, match="未找到 Tesseract"):
        ocr.ocr_bytes(b"png")


def test_ocr_bytes_process_failure(fake_tesseract):
    def failing_run(args, **kw):
        return _FakeProc(returncode=1, stderr=b"Error opening data file")

    import app.core.ocr as m
    orig = m.subprocess.run
    m.subprocess.run = failing_run
    try:
        with pytest.raises(ocr.OCRError, match="OCR 失败"):
            ocr.ocr_bytes(b"png")
    finally:
        m.subprocess.run = orig


def test_ocr_bytes_timeout(monkeypatch, tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "tesseract.exe").write_bytes(b"fake")
    monkeypatch.setattr(ocr, "_vendor_dirs", lambda: [vendor])

    def hanging_run(args, **kw):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(ocr.subprocess, "run", hanging_run)
    with pytest.raises(ocr.OCRError, match="超时"):
        ocr.ocr_bytes(b"png")


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
#  集成：真实引擎端到端往返（无引擎/语言包的环境自动跳过）
# ------------------------------------------------------------------ #

# 夹具为真实桌面会话渲染的清晰文字图（offscreen QPA 无字体无法生成）
_OCR_FIXTURE = Path(__file__).parent / "data" / "ocr_fixture.png"


def _real_engine_available() -> bool:
    exe = ocr.find_tesseract()
    return exe is not None and (
        exe.parent / "tessdata" / "eng.traineddata"
    ).is_file()


@pytest.mark.skipif(
    not _real_engine_available() or not _OCR_FIXTURE.is_file(),
    reason="无 Tesseract 引擎/语言包或缺少夹具",
)
def test_real_ocr_roundtrip():
    """已知文字图像 → ocr_bytes → 断言识别出中英文关键词。"""
    text = ocr.ocr_bytes(_OCR_FIXTURE.read_bytes(), "chi_sim+eng")
    assert "Tram" in text, f"OCR 结果: {text!r}"
    assert "Hello" in text or "hello" in text.lower(), f"OCR 结果: {text!r}"
    assert "离线" in text, f"OCR 结果: {text!r}"
