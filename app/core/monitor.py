"""固定区域实时监控的核心漏斗逻辑（无 GUI / 无线程依赖）。

三步优化漏斗（便宜的先跑，贵的后跑）：
  ① 帧差法门控：当前帧与参考帧（上次产出新文本时的帧）降采样比对，
     相似则跳过 OCR——静态背景（游戏对话框等）下绝大多数周期在此挡掉；
  ② OpenCV 轻量预处理：灰度/对比度/亮度自适应反转/小区域放大，
     刻意保守（不做激进二值化，那会伤 RapidOCR 的自然图像检测率）；
  ③ 文本相似度查重 + 防抖：归一化后与最近 N 条已翻译文本比对，
     且要求连续 `debounce` 个周期文本稳定才产出，抑制渐入渐出动画
     中途的半截识别与 OCR 抖动误差。

视频等动态背景下帧差恒超阈值属预期：门控退化为恒通过，漏斗仍由
③ 保证不重复翻译。所有状态集中在 RegionMonitorState，供监控线程
串行喂帧；纯函数部分独立可单测。
"""

from __future__ import annotations

import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

# OCR 结果行类型：(文本, 置信度)
OcrFn = Callable[[Any], list[tuple[str, float]]]


@dataclass
class MonitorParams:
    """漏斗阈值与采集循环开关（config monitor 节同名字段为其来源）。"""

    interval_ms: int = 500  # 监控周期
    diff_threshold: float = 0.02  # 帧差比例超过此值视为画面变化
    similarity_threshold: float = 0.88  # 归一化文本相似度达到此值视为重复
    debounce: int = 2  # 连续 N 个周期文本稳定才产出
    history_size: int = 5  # 查重保留最近 N 条已翻译文本
    min_confidence: float = 0.6  # 低于此置信度的识别行丢弃
    min_chars: int = 2  # 识别文本短于此值视为无文字
    # 帧差比对前的统一缩放宽（像素），提速且对噪声更鲁棒
    diff_width: int = 320
    # 预处理：识别区域高度低于此值时放大
    upscale_height: int = 60
    # 鼠标位于监控区域内时跳过采样：光标本身不进 GDI 截图，但被监控
    # 程序响应鼠标渲染的悬停态（控制条/高亮/浮层）会误触发翻译
    pause_on_cursor: bool = True


# ------------------------------------------------------------------ #
#  纯函数（可独立单测）
# ------------------------------------------------------------------ #


def normalize_text(text: str) -> str:
    """相似度比对前的归一化：去空白/标点、全角转半角、大小写统一。

    字幕常见的排版抖动（多一个空格、标点有无、全半角差异）不应
    被当作新文本触发翻译。
    """
    # 全角 ASCII -> 半角
    half = unicodedata.normalize("NFKC", text)
    kept = [
        ch
        for ch in half
        if ch.isalnum()  # 只留字母数字（含中日韩文字），丢弃标点与空白
    ]
    return "".join(kept).lower()


def text_similar(a: str, b: str, threshold: float) -> bool:
    """归一化后的两段文本是否相似（SequenceMatcher 比率 >= threshold）。"""
    if not a or not b:
        return False  # 空文本（无字幕）由调用方按无文字处理，不参与查重
    return SequenceMatcher(None, a, b).ratio() >= threshold


def filter_lines(lines: list[tuple[str, float]], min_confidence: float) -> str:
    """按置信度过滤 OCR 行并拼接为文本；全部被过滤/无行返回 ""。"""
    kept = [t.strip() for t, score in lines if score >= min_confidence and t.strip()]
    return "\n".join(kept).strip()


def frame_diff_ratio(img_a: Any, img_b: Any, width: int) -> float:
    """两帧的差异比例：降采样灰度 absdiff 后，差异像素占比。

    任一帧为 None 时返回 1.0（视为全变，强制走后续流程）。
    差异像素定义：灰度差 > 25（8bit），容忍压缩噪声与轻微抖动。
    """
    import cv2
    import numpy as np

    if img_a is None or img_b is None:
        return 1.0
    a = _to_gray_small(img_a, width)
    b = _to_gray_small(img_b, width)
    if a.shape != b.shape:
        return 1.0  # 尺寸不一致（区域被调整等），视为变化
    diff = cv2.absdiff(a, b)
    return float(np.count_nonzero(diff > 25)) / float(diff.size)


def _to_gray_small(img: Any, width: int) -> Any:
    """统一缩放到指定宽的灰度图，供帧差比对。"""
    import cv2

    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)  # type: ignore[attr-defined]
    h, w = gray.shape[:2]
    if w > width:
        scale = width / w
        gray = cv2.resize(gray, (width, max(1, round(h * scale))))
    return gray


def preprocess(img: Any, upscale_height: int) -> Any:
    """OCR 前轻量预处理：亮度自适应反转 + 小区域放大（保守策略）。

    返回 BGR ndarray。不做二值化/锐化：RapidOCR 检测模型在自然
    图像上训练，过度加工反而降低召回。
    """
    import cv2

    out = img
    h, w = out.shape[:2]
    if 0 < h < upscale_height:
        scale = 3 if h < 20 else 2
        out = cv2.resize(out, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    # 深底浅字更常见于字幕，但浅底深字也存在：均值判断后把浅底统一
    # 反转为深底，减少识别模型对配色的敏感（仅反转，不改对比度）
    import numpy as np

    if float(out.mean()) > 127.0:
        out = cv2.bitwise_not(out)
    return np.ascontiguousarray(out)


# ------------------------------------------------------------------ #
#  漏斗状态机
# ------------------------------------------------------------------ #


@dataclass
class RegionMonitorState:
    """串行喂帧的漏斗状态机：process(frame) -> 需翻译的新文本或 None。

    frame 为 BGR ndarray（调用方保证线程串行，本类自身无线程安全
    要求）。参考帧语义：上次产出新文本（或确认无文字）时的帧，
    与之相似即认为画面未变，直接跳过 OCR。
    """

    params: MonitorParams = field(default_factory=MonitorParams)
    ocr: OcrFn = lambda img: []  # 注入识别函数，测试时可打桩

    _ref_frame: Any = None
    _history: deque[str] = field(default_factory=deque)  # 最近 N 条已产出的归一化文本
    _pending_norm: str = ""  # 防抖中的候选文本（归一化）
    _pending_count: int = 0  # 候选已连续出现的周期数

    def process(self, frame: Any) -> str | None:
        """喂入一帧，返回需要翻译的原文（未归一化）；None 表示无新文本。

        抛出的 OCRError 由调用方（监控线程）兜底转错误信号。
        """
        p = self.params
        # ① 帧差法门控
        if frame_diff_ratio(frame, self._ref_frame, p.diff_width) < p.diff_threshold:
            return None

        # ② 预处理 + OCR（③ 的置信度过滤在 filter_lines 内）
        lines = self.ocr(preprocess(frame, p.upscale_height))
        text = filter_lines(lines, p.min_confidence)
        norm = normalize_text(text)
        if len(norm) < max(p.min_chars, 1):
            # 无有效文字（字幕消失/纯背景）：更新参考帧，避免静态
            # 空区域每个周期都重跑 OCR；同时清掉防抖候选
            self._accept_frame(frame)
            self._pending_norm = ""
            self._pending_count = 0
            return None

        # ③ 相似度查重（对最近 N 条 + 当前防抖候选之外的闪回）
        for prev in self._history:
            if text_similar(norm, prev, p.similarity_threshold):
                self._accept_frame(frame)  # 老内容重现：不翻译，只对齐参考帧
                self._pending_norm = ""
                self._pending_count = 0
                return None

        # 防抖：候选需连续 debounce 个周期稳定
        if norm == self._pending_norm:
            self._pending_count += 1
        else:
            self._pending_norm = norm
            self._pending_count = 1
        if self._pending_count < p.debounce:
            return None

        # 产出新文本
        self._accept_frame(frame)
        self._pending_norm = ""
        self._pending_count = 0
        self._history.append(norm)
        while len(self._history) > self.params.history_size:
            self._history.popleft()
        return text

    def _accept_frame(self, frame: Any) -> None:
        self._ref_frame = frame
