r"""Tesseract OCR 封装（Windows）。

自写轻量子进程封装，不引入 pytesseract/Pillow 依赖。
只依赖 PyQt6 的 QPixmap 做图像预处理（转 PNG 字节流），
真正的识别在 ocr_bytes 中完成，可脱离 GUI 单测。

Tesseract 二进制定位顺序：
  ① 打包内置（PyInstaller onefile 解包目录）vendor/tesseract/
  ② 开发环境仓库内 vendor/tesseract/
  ③ PATH 中的 tesseract
  ④ 常见安装目录 %ProgramFiles%\Tesseract-OCR
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QPixmap

logger = logging.getLogger(__name__)

# 子进程超时：正常截图识别 < 2s，10s 兜底防挂死
OCR_TIMEOUT_S = 10
# 页面分割模式：3 = 全自动分割（PSM_AUTO）
PSM_AUTO = "3"
# 选区高度（设备无关像素）低于此值时放大，提升小字号识别率
_MIN_UPSCALE_HEIGHT = 60


class OCRError(Exception):
    """OCR 执行失败（二进制缺失、子进程崩溃、超时等）。"""


def _vendor_dirs() -> list[Path]:
    """候选 vendor/tesseract 目录：打包内置 + 仓库内。"""
    dirs: list[Path] = []
    # ① PyInstaller onefile：datas 解包到 _MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "vendor" / "tesseract")
    # ② 开发环境：app/core/ocr.py -> 仓库根/vendor/tesseract
    dirs.append(Path(__file__).resolve().parents[2] / "vendor" / "tesseract")
    return dirs


def find_tesseract() -> Path | None:
    """定位 tesseract.exe，未找到返回 None（调用方负责托盘通知引导）。"""
    for d in _vendor_dirs():
        exe = d / "tesseract.exe"
        if exe.is_file():
            return exe
    # ③ PATH
    which = shutil.which("tesseract")
    if which:
        return Path(which)
    # ④ 常见安装目录（winget/choco/UB Mannheim 安装器默认路径）。
    # Windows 环境变量名不区分大小写（os.environ 为大小写不敏感映射）
    candidates = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
    ]
    for base in candidates:
        if base:
            exe = Path(base) / "Tesseract-OCR" / "tesseract.exe"
            if exe.is_file():
                return exe
    return None


def _build_args(exe: Path, png_path: Path, languages: str) -> list[str]:
    """构造 tesseract 命令行参数。"""
    return [
        str(exe),
        str(png_path),
        "stdout",  # 识别结果写到 stdout
        "-l",
        languages,
        "--psm",
        PSM_AUTO,
    ]


def _env_for(exe: Path) -> dict[str, str]:
    """构造子进程环境变量。

    vendor 目录中的独立 exe 没有编译期 tessdata 路径，
    必须显式设置 TESSDATA_PREFIX 指向同目录下的 tessdata/。
    """
    env = os.environ.copy()
    tessdata = exe.parent / "tessdata"
    if tessdata.is_dir():
        env["TESSDATA_PREFIX"] = str(tessdata)
    return env


def clean_output(text: str) -> str:
    """清洗 OCR 原始输出：逐行去尾部空白、去末尾空行，保留行间换行。

    Tesseract 输出行常带尾随空格，行首缩进保留（可能是版面对齐信息）。
    """
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    # 去掉末尾连续空行
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def pixmap_to_png(pixmap: QPixmap) -> bytes:
    """QPixmap 转 PNG 字节流，附带预处理（选区过小时放大）。"""
    img = pixmap
    if 0 < img.height() < _MIN_UPSCALE_HEIGHT:
        scale = 3 if img.height() < 20 else 2
        img = img.scaled(
            img.width() * scale,
            img.height() * scale,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise OCRError("截图编码为 PNG 失败")
    # QByteArray.data() 返回 bytes（PyQt6），直接构造 bytes 避免 stub 重载歧义
    return bytes(buf.data().data())


def ocr_bytes(png: bytes, languages: str = "chi_sim+eng") -> str:
    """对 PNG 字节流执行 OCR，返回清洗后的文本。失败抛 OCRError。"""
    exe = find_tesseract()
    if exe is None:
        raise OCRError(
            "未找到 Tesseract，请安装（winget install UB-Mannheim.TesseractOCR）"
            "或运行 tools/fetch_tesseract.py"
        )
    # delete=False + finally unlink：Windows 上文件被 subprocess 打开期间
    # NamedTemporaryFile 上下文管理无法二次打开；这里我们自己先写完关闭
    fd, tmp = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        try:
            proc = subprocess.run(  # noqa: S603 参数由 _build_args 构造
                _build_args(exe, Path(tmp), languages),
                capture_output=True,
                timeout=OCR_TIMEOUT_S,
                env=_env_for(exe),
            )
        except subprocess.TimeoutExpired:
            raise OCRError(f"OCR 超时（>{OCR_TIMEOUT_S}s）") from None
        except OSError as e:
            raise OCRError(f"无法启动 tesseract: {e}") from e
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise OCRError(f"OCR 失败 (code {proc.returncode}): {stderr}")
        return clean_output(proc.stdout.decode("utf-8", errors="replace"))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def ocr_image(pixmap: QPixmap, languages: str = "chi_sim+eng") -> str:
    """对 QPixmap 执行 OCR（主线程调用，QPixmap 非 GUI 线程安全）。"""
    return ocr_bytes(pixmap_to_png(pixmap), languages)
