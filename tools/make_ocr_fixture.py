"""生成 OCR 集成测试夹具图像 tests/data/ocr_fixture.png（一次性工具）。

offscreen QPA 无字体，须在真实桌面会话（windows 平台）渲染清晰文字。
用法: python tools/make_ocr_fixture.py
"""

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "data" / "ocr_fixture.png"


def main() -> int:
    app = QApplication([])

    img = QImage(720, 180, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    painter = QPainter(img)
    painter.setPen(QColor("black"))
    painter.setFont(QFont("Microsoft YaHei", 22))
    painter.drawText(QRect(24, 12, 680, 70), 0, "离线翻译 Tram 识图")
    painter.drawText(QRect(24, 96, 680, 70), 0, "Hello OCR World 2026")
    painter.end()

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(FIXTURE), "PNG")
    print(f"fixture saved: {FIXTURE} ({FIXTURE.stat().st_size} bytes)")

    # 立即用真实引擎回验一次
    sys.path.insert(0, str(REPO_ROOT))
    from app.core.ocr import find_tesseract, ocr_bytes

    exe = find_tesseract()
    if exe is None:
        print("warning: tesseract not found, skip roundtrip check")
        return 0
    text = ocr_bytes(FIXTURE.read_bytes(), "chi_sim+eng")
    print("roundtrip OCR result:")
    print(text)
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
