"""RapidOCR 引擎封装（PaddleOCR 模型的 ONNX 版）。

引擎为 pip 可选依赖（rapidocr + onnxruntime），模型随 wheel 内置，
离线开箱即用；识别为进程内推理，无子进程与临时文件落盘。
截图使用 mss 库（可选依赖，随 ocr extra 一并安装），直接出 BGR ndarray，
跳过 PNG 编解码，速度更快且支持多显示器。
"""

from __future__ import annotations

import importlib.util
import logging
import threading
from typing import Any

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QPixmap

logger = logging.getLogger(__name__)

# 选区高度（设备无关像素）低于此值时放大，提升小字号识别率
_MIN_UPSCALE_HEIGHT = 60

# 支持的 OCR 语言码：全部走 PP-OCRv6 multi 内置模型（中/英/日 + 欧洲各语种），
# 无需切换专用模型。列表用于校验配置合法性，实际识别由模型自动判断语言。
# 韩语/俄语等需专用 rec 模型的语言暂未引入。
_SUPPORTED_LANGS = frozenset({
    "ch",
    "eng",
    "jpn",
    "japan",
    "ja",
    "chi_sim+eng",
    "chi_tra+eng",
})


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
        raise OCRError(f"OCR 语言码不支持: {languages}")


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


# ------------------------------------------------------------------ #
#  屏幕截图（mss）
# ------------------------------------------------------------------ #


def is_mss_available() -> bool:
    """mss 截图库是否可用。"""
    return importlib.util.find_spec("mss") is not None


# mss 单例：避免每次截图都重新创建设备上下文（D3D/GDI 句柄）
_mss_instance: Any = None
_mss_lock = threading.Lock()


def _get_mss() -> Any:
    """懒加载 mss 单例。"""
    global _mss_instance
    if _mss_instance is None:
        with _mss_lock:
            if _mss_instance is None:
                try:
                    from mss import MSS

                    _mss_instance = MSS()
                except Exception as e:
                    raise OCRError(f"截图引擎初始化失败: {e}") from e
    return _mss_instance


def capture_primary_screen() -> tuple[Any, dict]:
    """截取主屏，返回 (BGR ndarray, monitor_info)。

    monitor_info 包含 left/top/width/height 等物理坐标，
    用于裁剪时的坐标换算。
    失败抛 OCRError。
    """
    import cv2
    import numpy as np

    if not is_mss_available():
        raise OCRError('mss 未安装，请运行 pip install "tram[ocr]"')

    sct = _get_mss()
    try:
        # monitors[0] 是所有屏幕的拼接虚拟屏，monitors[1] 是主屏
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
    except Exception as e:
        raise OCRError(f"屏幕截图失败: {e}") from e

    # mss grab 返回 BGRA 的 MSSImage（类 ndarray 的对象）
    arr = np.array(raw, dtype=np.uint8)
    # BGRA -> BGR（去掉 alpha 通道，cv2/OCR 都用 BGR）
    bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    return bgr, monitor


def ndarray_to_qpixmap(bgr: Any) -> QPixmap:
    """BGR ndarray 转 QPixmap，用于覆盖层背景显示。

    只在主线程调用（给 RegionOverlay 用）。
    """
    import cv2

    if bgr is None or bgr.size == 0:
        return QPixmap()
    # BGR -> RGB（Qt 用 RGB）
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
    # QImage 只是借用 numpy 缓冲区，必须 copy() 成 QPixmap 后再释放
    return QPixmap.fromImage(qimg.copy())


def crop_and_upscale(
    bgr: Any, x: int, y: int, w: int, h: int
) -> Any:
    """从全屏 BGR ndarray 中裁剪指定区域，过小则放大以提升识别率。

    x, y, w, h 为物理像素坐标（与 monitor_info 同坐标系）。
    返回裁剪并可能放大后的 BGR ndarray。
    """
    import cv2
    import numpy as np

    if w <= 0 or h <= 0:
        return np.zeros((1, 1, 3), dtype=np.uint8)

    # 裁剪（numpy 切片就是引用，这里做拷贝避免引用原图大内存）
    cropped = bgr[y : y + h, x : x + w].copy()

    # 高度过低时放大，提升小字号识别率
    # 使用 INTER_LINEAR 而非 INTER_CUBIC：速度快约 40%，
    # 放大 2-3 倍时 OCR 识别率差异可忽略（PP-OCR 自身有
    # 图像预处理与特征提取，插值差异在下游被抹平）
    if 0 < cropped.shape[0] < _MIN_UPSCALE_HEIGHT:
        scale = 3 if cropped.shape[0] < 20 else 2
        cropped = cv2.resize(
            cropped,
            (cropped.shape[1] * scale, cropped.shape[0] * scale),
            interpolation=cv2.INTER_LINEAR,
        )
    return cropped


def ocr_ndarray(img: Any, languages: str = "ch") -> str:
    """对 BGR ndarray 执行 OCR，返回清洗后的文本；无文字返回 ""。

    直接走 ndarray 路径，跳过 PNG 编解码，比 ocr_bytes 更快。
    失败抛 OCRError。
    """
    lines = ocr_lines(img, languages)
    return clean_output("\n".join(t for t, _score in lines))
