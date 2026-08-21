"""RapidOCR 引擎封装（PaddleOCR 模型的 ONNX 版）。

引擎为 pip 可选依赖（rapidocr + onnxruntime），模型随 wheel 内置，
离线开箱即用；识别为进程内推理，无子进程与临时文件落盘。
只依赖 PyQt6 的 QPixmap 做图像预处理（转 PNG 字节流），
真正的识别在 ocr_bytes 中完成，可脱离 GUI 单测。
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QPixmap

logger = logging.getLogger(__name__)

# 选区高度（设备无关像素）低于此值时放大，提升小字号识别率
_MIN_UPSCALE_HEIGHT = 60

# 支持的语言码：RapidOCR 的 `ch` 模型即中英混排模型，对纯英文也够用，
# 故 eng 一并归入（旧 Tesseract 语言码兼容迁移）。当前全部落到引擎默认
# 模型，无需映射值；v2 引入多语言模型时再升级为 {码: 模型} 映射。
# jpn/kor 等专用模型暂不引入（路线图）。
_SUPPORTED_LANGS = frozenset({"ch", "eng", "chi_sim+eng", "chi_tra+eng"})


class OCRError(Exception):
    """OCR 执行失败（引擎未安装、初始化失败、推理异常等）。"""


def is_rapidocr_available() -> bool:
    """rapidocr/onnxruntime 是否已安装（OCR 为可选依赖）。

    只查模块 spec、不真正 import：该检查在热键预检路径（UI 线程）
    调用，真 import 会把 numpy/opencv 等重依赖一并拖进来。
    """
    return (
        importlib.util.find_spec("rapidocr") is not None
        and importlib.util.find_spec("onnxruntime") is not None
    )


_engine: Any = None
_engine_lock = threading.Lock()


def _get_engine() -> Any:
    """懒加载 RapidOCR 单例：首次 init 数秒（加载模型），之后复用。

    double-check 锁只护 init、不护推理：onnxruntime 的 session Run
    自身线程安全，当前也仅单 OCRWorker 串行调用。不要把这把锁扩大
    到包住整个 ocr_bytes，那会把识别串行化。
    init 失败不缓存结果，下次调用可重试。
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                try:
                    from rapidocr import RapidOCR

                    # 引擎 INFO 日志（模型路径等）对 GUI 应用是噪音，静音。
                    # rapidocr 各模块懒加载时会重复实例化 Logger 并把
                    # logger 级别重置回 INFO，故从 handler 侧降级才持久
                    # （handler 只添加一次，不会被重置）。
                    rapid_logger = logging.getLogger("RapidOCR")
                    for handler in rapid_logger.handlers:
                        handler.setLevel(logging.WARNING)
                    rapid_logger.setLevel(logging.WARNING)
                    _engine = RapidOCR()
                except Exception as e:
                    raise OCRError(f"OCR 引擎初始化失败: {e}") from e
    return _engine


def clean_output(text: str) -> str:
    """清洗 OCR 原始输出：逐行去尾部空白、去末尾空行，保留行间换行。"""
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


def ocr_bytes(png: bytes, languages: str = "ch") -> str:
    """对 PNG 字节流执行 OCR，返回清洗后的文本；无文字返回 ""。

    失败抛 OCRError。不设硬超时：进程内推理无法外部 kill，而 ONNX
    推理有界（正常截图 < 2s）；popup「识别中…」已覆盖等待体验，
    极端情况 v2 再议。
    """
    # 环境/语言校验先于解码：引擎未安装时根因是缺依赖，
    # 不应被「字节流解码失败」之类的下游错误掩盖
    _validate_env(languages)
    lines = ocr_lines(png_to_ndarray(png), languages)
    return clean_output("\n".join(t for t, _score in lines))


def _validate_env(languages: str) -> None:
    """引擎可用性与语言码校验，失败抛 OCRError（ocr_bytes/ocr_lines 共用）。"""
    if not is_rapidocr_available():
        raise OCRError('OCR 引擎未安装，请运行 pip install "tram[ocr]"')
    if languages.strip().lower() not in _SUPPORTED_LANGS:
        raise OCRError(f"OCR 语言暂不支持: {languages}（当前仅 ch 中英混排）")


def png_to_ndarray(png: bytes) -> Any:
    """PNG 字节流解码为 BGR ndarray（cv2.imdecode）。

    在 worker 线程以 ndarray 形式做识别，避免与 QPixmap（仅主线程
    可用）耦合。cv2/numpy 是 rapidocr 的必然依赖，此处不再探测可用性。
    """
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise OCRError("PNG 字节流解码失败")
    return img


def ocr_lines(img: Any, languages: str = "ch") -> list[tuple[str, float]]:
    """对 BGR ndarray 执行 OCR，返回 [(文本, 置信度), ...]。

    带置信度以便调用方按行过滤低质量识别。识别失败抛 OCRError；
    无文字返回空列表。
    """
    _validate_env(languages)
    engine = _get_engine()
    try:
        result = engine(img)
    except Exception as e:
        raise OCRError(f"OCR 识别失败: {e}") from e
    if result is None or not result.txts:
        return []
    txts = result.txts
    scores = result.scores if result.scores is not None else ()
    return [
        (t, float(scores[i]) if i < len(scores) else 0.0)
        for i, t in enumerate(txts)
    ]
