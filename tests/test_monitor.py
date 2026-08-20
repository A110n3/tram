"""区域监控漏斗核心逻辑测试（OCR 打桩，cv2 用真实小图）。"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.monitor import (
    MonitorParams,
    RegionMonitorState,
    filter_lines,
    frame_diff_ratio,
    normalize_text,
    preprocess,
    text_similar,
)


def make_frame(color: int, size: tuple[int, int] = (100, 400)) -> np.ndarray:
    """纯色 BGR 帧。"""
    h, w = size
    return np.full((h, w, 3), color, dtype=np.uint8)


def draw_text_like(frame: np.ndarray, value: int) -> np.ndarray:
    """在帧中画一块高对比"文字"区域，制造帧差。"""
    out = frame.copy()
    out[10:30, 10:100] = value
    return out


# ------------------------------------------------------------------ #
#  纯函数
# ------------------------------------------------------------------ #


def test_normalize_text_fullwidth_punct_and_case():
    assert normalize_text("Hello， World！") == "helloworld"
    assert normalize_text("你好 世界") == "你好世界"
    assert normalize_text("Ｔｅｓｔ１２３") == "test123"


def test_normalize_text_empty():
    assert normalize_text("！！ ？？") == ""


def test_text_similar_true_false():
    assert text_similar("helloworld", "helloworld", 0.88) is True
    assert text_similar("helloworld", "xxyyzz", 0.88) is False
    assert text_similar("", "abc", 0.88) is False


def test_text_similar_ocr_noise():
    # OCR 抖动：多识别/漏识别一两个标点或空白后仍应判为相似
    assert text_similar(normalize_text("你好，世界！"), normalize_text("你好 世界"), 0.88)


def test_filter_lines_drops_low_confidence():
    lines = [("好", 0.9), ("坏", 0.3), (" ", 0.95), ("行", 0.88)]
    assert filter_lines(lines, 0.6) == "好\n行"
    assert filter_lines([], 0.6) == ""


def test_frame_diff_ratio_identical_and_changed():
    a = draw_text_like(make_frame(50), 255)
    assert frame_diff_ratio(a, a.copy(), 320) == pytest.approx(0.0)
    b = draw_text_like(make_frame(50), 0)
    assert frame_diff_ratio(a, b, 320) > 0.02


def test_frame_diff_ratio_none_frame():
    a = make_frame(50)
    assert frame_diff_ratio(a, None, 320) == 1.0
    assert frame_diff_ratio(None, None, 320) == 1.0


def test_frame_diff_ratio_shape_mismatch():
    assert frame_diff_ratio(make_frame(50), make_frame(50, (80, 300)), 320) == 1.0


def test_frame_diff_ratio_noise_tolerance():
    # 轻微噪声（灰度差 <= 25）不应计入差异
    a = make_frame(50)
    b = a.copy()
    b[0:10, 0:10] += 20
    assert frame_diff_ratio(a, b, 320) == pytest.approx(0.0)


def test_preprocess_upscales_small_region():
    img = np.full((30, 200, 3), 200, dtype=np.uint8)
    out = preprocess(img, 60)
    assert out.shape[0] == 60  # x2 放大


def test_preprocess_inverts_light_background():
    img = np.full((100, 200, 3), 230, dtype=np.uint8)  # 浅底
    out = preprocess(img, 60)
    assert float(out.mean()) < 127


def test_preprocess_keeps_dark_background():
    img = np.full((100, 200, 3), 20, dtype=np.uint8)
    out = preprocess(img, 60)
    assert float(out.mean()) < 127


# ------------------------------------------------------------------ #
#  状态机漏斗
# ------------------------------------------------------------------ #


class StubOCR:
    """打桩识别器：脚本化返回 (文本, 置信度) 列表序列。"""

    def __init__(self, script: list[list[tuple[str, float]]]):
        self.script = list(script)
        self.calls: list[np.ndarray] = []

    def __call__(self, img):
        self.calls.append(img)
        return self.script.pop(0) if self.script else []


def test_static_background_skips_ocr():
    """帧差法门控：与参考帧相似时不跑 OCR。"""
    stub = StubOCR([])
    state = RegionMonitorState(ocr=stub)
    base = draw_text_like(make_frame(50), 255)
    state.process(base)  # 首帧：OCR 跑一次（无文字 -> 参考帧更新）
    assert len(stub.calls) == 1
    state.process(base.copy())  # 相似帧：被门控挡掉
    assert len(stub.calls) == 1


def test_first_text_emitted_after_debounce():
    stub = StubOCR([[("你好世界", 0.9)], [("你好世界", 0.9)]])
    state = RegionMonitorState(ocr=stub)
    f1 = draw_text_like(make_frame(50), 255)
    assert state.process(f1) is None  # 第 1 周期：进入防抖
    f2 = draw_text_like(make_frame(60), 255)  # 背景微变（超阈值）
    assert state.process(f2) == "你好世界"  # 第 2 周期：稳定产出


def test_debounce_resets_on_change():
    """防抖期间文本变化：计数重置，不产出半截文字。"""
    stub = StubOCR([[("你", 0.9)], [("你好", 0.9)], [("再见", 0.9)], [("再见", 0.9)]])
    state = RegionMonitorState(ocr=stub)
    assert state.process(draw_text_like(make_frame(50), 255)) is None
    assert state.process(draw_text_like(make_frame(100), 255)) is None  # 文本变了，重置
    assert state.process(draw_text_like(make_frame(150), 255)) is None  # 重新计 1
    assert state.process(draw_text_like(make_frame(200), 255)) == "再见"


def test_history_dedup_blocks_repeated_text():
    """A->B->A 闪回：A 重现不重复翻译。"""
    stub = StubOCR(
        [
            [("AAAA", 0.9)], [("AAAA", 0.9)],  # A 稳定产出
            [("BBBB", 0.9)], [("BBBB", 0.9)],  # B 稳定产出
            [("AAAA", 0.9)],  # A 重现 -> 查重拦截
        ]
    )
    state = RegionMonitorState(ocr=stub)
    assert state.process(draw_text_like(make_frame(50), 255)) is None  # A 防抖中
    assert state.process(draw_text_like(make_frame(100), 255)) == "AAAA"  # A 产出
    assert state.process(draw_text_like(make_frame(70), 100)) is None  # B 防抖中
    assert state.process(draw_text_like(make_frame(120), 0)) == "BBBB"  # B 产出
    assert state.process(draw_text_like(make_frame(170), 255)) is None  # A 重现被拦


def test_empty_text_updates_reference():
    """字幕消失：无文字时更新参考帧，静态空区域不再跑 OCR。"""
    stub = StubOCR([[("gone", 0.9)], [("gone", 0.9)], []])
    state = RegionMonitorState(ocr=stub)
    f = draw_text_like(make_frame(50), 255)
    state.process(f)
    state.process(draw_text_like(make_frame(60), 255))  # 产出 "gone"
    empty = make_frame(50)
    assert state.process(empty) is None  # 文字消失
    assert state.process(empty.copy()) is None  # 相似于参考帧，门控挡掉
    assert len(stub.calls) == 3


def test_min_chars_treated_as_empty():
    stub = StubOCR([[("a", 0.9)], [("a", 0.9)]])
    state = RegionMonitorState(ocr=stub)
    state.process(draw_text_like(make_frame(50), 255))
    state.process(draw_text_like(make_frame(100), 255))  # 超噪声阈值的背景变化
    assert len(stub.calls) == 2
    # "a"（归一化后 1 字符）低于 min_chars=2，两帧都按无文字处理，无产出


def test_low_confidence_lines_dropped():
    stub = StubOCR([[("噪声", 0.3), ("真实文字", 0.9)]] * 2)
    state = RegionMonitorState(ocr=stub)
    state.process(draw_text_like(make_frame(50), 255))
    assert state.process(draw_text_like(make_frame(60), 255)) == "真实文字"


def test_params_from_config():
    p = MonitorParams(interval_ms=1000, similarity_threshold=0.9)
    state = RegionMonitorState(params=p, ocr=lambda _img: [])
    assert state.params.interval_ms == 1000
