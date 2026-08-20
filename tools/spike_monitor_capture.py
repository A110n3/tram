"""监控截图路径冒烟：PIL ImageGrab 后台线程截图 + RapidOCR ndarray 输入。"""
import threading
import time

import numpy as np
from PIL import ImageGrab
from rapidocr import RapidOCR

# 1) ImageGrab 是否可在非 GUI 线程工作，返回尺寸是否符合预期
result = {}


def grab():
    t0 = time.perf_counter()
    img = ImageGrab.grab(bbox=(100, 100, 500, 200))  # 400x100 物理像素
    dt = (time.perf_counter() - t0) * 1000
    result["size"] = img.size
    result["ms"] = round(dt, 1)
    result["arr"] = np.asarray(img)


th = threading.Thread(target=grab)
th.start()
th.join()
print(
    "ImageGrab:", result["size"], result["ms"],
    "ms, dtype:", result["arr"].dtype, "shape:", result["arr"].shape,
)

# 2) RapidOCR 接受 BGR ndarray 吗（喂一张自绘文字图，识别可为空但不抛错）
engine = RapidOCR()
arr = result["arr"][:, :, ::-1].copy()  # RGB -> BGR
t0 = time.perf_counter()
out = engine(arr)
ms = (time.perf_counter() - t0) * 1000
print(f"OCR ndarray ok, {ms:.0f} ms, txts={out.txts!r} scores={out.scores!r}")

# 3) 全屏抓取耗时（评估另一种方案）
t0 = time.perf_counter()
full = ImageGrab.grab()
full_ms = (time.perf_counter() - t0) * 1000
print("full grab:", full.size, f"{full_ms:.1f} ms")
