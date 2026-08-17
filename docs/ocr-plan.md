# Tram v0.3.0 — 用 RapidOCR（PaddleOCR 模型 ONNX 版）实现 OCR 识图翻译

> 状态：**已实施**（2026-08-17，分支 `OCR-RapidOCR`）。前版 Tesseract 方案存档于远程分支 `OCR-Tesseract`。
>
> 实施中的实测修正：① rapidocr 3.9.2 实际内置默认为 **PP-OCRv6 small** det/rec 模型（冒烟日志证实），非下文调研时假定的 PP-OCRv4；② 顺带修复 pyproject build-backend（`setuptools.backends._legacy` 在 setuptools 82 被移除，改为标准 `setuptools.build_meta`），否则 `pip install .` / CI 全挂。

## Context（背景）

用户决定放弃 Tesseract 路线（已存档于远程分支 `OCR-Tesseract`），改用 **PaddleOCR** 重做识图翻译；main 已回退到 OCR 之前的 v0.2.4（`6d150cb`，与 origin/main 一致）。经调研确认：官方 paddlepaddle **无 Python 3.14 wheel**（本机 3.14.5），而 **RapidOCR**（PaddleOCR PP-OCRv4 模型的官方 ONNX 转换分发）全部依赖在 3.14 上 wheel 就绪（rapidocr 3.9.2 py3-none-any 26MB 模型内置、onnxruntime 1.28 cp314、pyclipper/shapely/numpy cp314、opencv-python cp37-abi3 全兼容）。用户拍板：**RapidOCR 路线 + 先冒烟验证再接线**。

相比 Tesseract 版的核心收益：引擎即 pip 依赖（无 vendor/ 供给链）、模型内置于 wheel（离线开箱即用、CI 零额外步骤）、无子进程（无需临时文件/超时杀进程）。

**保留的抽象边界**：`app/core/ocr.py` 暴露 `ocr_bytes(png: bytes, languages: str) -> str` + `OCRError` + `pixmap_to_png` + `clean_output`，上层 UI（编排器/Worker/覆盖层）与引擎解耦，从 `OCR-Tesseract` 分支文件级复用。

---

## 阶段一：冒烟验证（先做，通过才进阶段二）

不写任何 app/ 代码。所有命令在 Git Bash、仓库根目录执行。

1. **装引擎到项目 venv**：`uv pip install rapidocr onnxruntime`
   - 通过标准：无报错；`uv pip list` 出现 rapidocr 3.9.x + onnxruntime ≥1.24
2. **提取夹具图**（中英文混排，引擎无关资产）：
   `mkdir -p tests/data && git show OCR-Tesseract:tests/data/ocr_fixture.png > tests/data/ocr_fixture.png`（~11.8KB，此文件阶段二直接入库）
3. **写一次性脚本 `tools/spike_rapidocr.py`** 并用 `uv run python tools/spike_rapidocr.py` 运行（沙箱限制：根目录直接 `python xxx.py` 会被拦，`python tools/x.py` / `uv run` 形式可用）：
   - 读夹具 PNG → `from rapidocr import RapidOCR; engine = RapidOCR()` → `result = engine(png_bytes)`
   - 运行前设置 `HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9`，做实「init 不触网」门槛（模型若需联网下载会立刻连接失败）
   - 打印 init 耗时、识别耗时、`result.txts` 逐行内容
   - 断言关键词 `Tram / 离线 / 翻译 / Hello / OCR / World / 2026` 全部命中，否则 exit 1
4. **冒烟门槛**：

   | 指标 | 门槛 |
   |------|------|
   | RapidOCR() 初始化 | 成功且不触网（模型 wheel 内置） |
   | init 耗时 | < 5s |
   | 单次识别耗时 | < 2s |
   | 中英文关键词 | 全部识别出 |

5. **失败回退**：质量差 → 试 SERVER 档模型；import/兼容失败 → 回 OCR-Tesseract 分支方案并与用户汇报
6. 冒烟脚本用完即删（不入库）；记录 init/识别耗时供后续参考

---

## 阶段二：完整接线（冒烟通过后）

### 分支

`git checkout -b OCR-RapidOCR main`

### 文件复用（自 OCR-Tesseract 分支 `git checkout OCR-Tesseract -- <path>`）

- **原样取回**：`app/ui/region_overlay.py`、`app/ui/popup.py`、`app/ui/worker.py`（OCRWorker 只依赖 ocr_bytes 签名）、`app/ui/main_window.py`（托盘菜单/生命周期接线）、`app/ui/settings_dialog.py`（OCR 分组）、`app/config.py`、`tests/test_config.py`、`tests/data/ocr_fixture.png`（已就位）、`tools/make_ocr_fixture.py`（回验段小改：find_tesseract → is_rapidocr_available）
- **取回后微调**：`app/ui/ocr_translator.py` 仅两处——L23 import 与 L134 热键预检 `find_tesseract()` 改为 `is_rapidocr_available()`，提示文案改为「OCR 引擎未安装，请运行 pip install "tram[ocr]"」
- **不取回**（Tesseract 专属）：`tools/fetch_tesseract.py`、分支版 `docs/ocr-plan.md`、分支版 `.gitignore` vendor 行、分支版 tram.spec/release.yml/pyproject/README/test_ocr.py

### 重写 `app/core/ocr.py`（核心，~80 行）

- 保留接口：`OCRError`、`pixmap_to_png`（含 <60px 放大预处理，原逻辑搬回）、`clean_output`、`ocr_bytes(png, languages="ch") -> str`
- 新增 `is_rapidocr_available() -> bool`（try import rapidocr + onnxruntime）
- 引擎为**线程安全懒加载单例**：模块级 `_engine` + `threading.Lock()` double-check（OCRWorker 在 QThread 调用；首次 init 数秒，之后复用）。
  **锁只护 init、不护推理**（onnxruntime 的 Run 自身线程安全；当前仅单 Worker，串行无影响），docstring 注明防止后人把整个 `ocr_bytes` 包进锁
- `ocr_bytes`：`engine(png)` → `result is None or not result.txts` 返回 `""` → 否则 `clean_output("\n".join(result.txts))`；异常包装为 `OCRError`
- **不设硬超时**（进程内推理无法外部 kill；ONNX 推理有界，正常截图 <2s；popup「识别中…」已覆盖等待体验，极端情况 v2 再议）——写入 docstring
- **语言映射** `_LANG_ALIASES`：`ch` 为新默认（PP-OCRv4 ch 模型 = 中英混排）；旧 Tesseract 值兼容迁移 `chi_sim+eng`/`chi_tra+eng`/`eng` → `ch`；jpn/kor 等预留不支持（路线图）。`eng` 也归 `ch` 为有意取舍：ch 模型对纯英文够用；若 v2 在意可切 en 专用模型
- 删除 find_tesseract/_vendor_dirs/_build_args/_env_for/OCR_TIMEOUT_S/PSM_AUTO

### 配置与版本

- `OCRConfig.languages` 默认 `"chi_sim+eng"` → `"ch"`（含注释说明 RapidOCR 语言码）；`_TYPE_COERCIONS` ocr 段不变
- `pyproject.toml`：`version 0.2.4 → 0.3.0`；新增 extras：`ocr = ["rapidocr>=3.9,<4", "onnxruntime>=1.24.1"]`（rapidocr 2→3 换过 `result.txts` API，上界挡 breaking change）（不放主依赖：纯划词用户不被迫装 ~150MB；1.24.1 是 cp314 wheel 起点，CI 3.12 亦满足）；`app/config.py` APP_VERSION 回退值同步 0.3.0

### 打包与 CI

- `tram.spec`：移除 vendor datas 段，改 `collect_all("rapidocr")` + `collect_all("onnxruntime")`（收集 .onnx 模型数据 + DLL/pyd + hiddenimports），装不上时 WARNING 降级
- `release.yml`：**删除** choco tesseract + fetch_tesseract 步骤，安装改 `pip install ".[dev,ocr]"`（模型随 wheel，零供给链）
- `ci.yml`：安装改 `pip install ".[dev,ocr]"` → 真实引擎集成测试在 CI 直接可跑（无需 skipif 引擎安装）

### 测试改写 `tests/test_ocr.py`

- 保留：clean_output 3 用例、pixmap_to_png 放大用例（逻辑未变）
- 删除：全部 Tesseract 专属（_build_args/find_tesseract/subprocess mock/TESSDATA_PREFIX/超时）
- 新增：`is_rapidocr_available` 正反用例（monkeypatch import）；`ocr_bytes` mock 单例用例（monkeypatch `_engine`：成功/空结果/抛异常→OCRError）；旧配置 `chi_sim+eng` 别名映射用例；`test_real_ocr_roundtrip` 保留（skipif 改为 rapidocr 可导入 + 夹具存在）
- `tests/test_config.py`：languages 默认值断言改 `"ch"`

### 文档

- README：功能描述改「内置 PP-OCRv4 中英文模型」、安装改 `pip install ".[ocr]"`、删 winget/fetch_tesseract 段、路线图 v0.3.0 条目改为 RapidOCR 表述、注明 rapidocr/onnxruntime 为可选依赖
- `.gitignore`：不需要 vendor/ 行（基线本就没有）；本机 `vendor/`（~80MB Tesseract）加入 `.git/info/exclude` 防手滑 `git add .` 误入库（仓库本地排除，不污染提交历史）

### 提交粒度（7 个 commit）

1. `feat(ocr): RapidOCR 引擎核心封装`（core/ocr.py）
2. `feat(ocr): UI 脚手架复用`（region_overlay/ocr_translator/popup/worker）
3. `feat(ocr): 配置与托盘/设置接线`（config/main_window/settings_dialog）
4. `test(ocr): 测试改写 + 夹具入库`
5. `build(ocr): 依赖、打包与 CI`（pyproject/tram.spec/workflows）
6. `docs(ocr): README 与方案文档`
7. `release: v0.3.0`（版本号 + 全量验证后）

---

## 验证

1. **静态**：`uv run ruff check app/ tests/ tools/`、`uv run mypy app/`（既有 hideMessage stub 误报除外）
2. **测试**：`uv run pytest tests/ -v`（含真实引擎集成用例，本机与 CI 均应通过）
3. **实机运行时**（项目铁律）：`uv run python -m app.main` → 托盘开启 OCR → `Ctrl+Shift+F4` 框选中英文 → 「识别中…」→ 流式译文；覆盖：重复框选命中缓存秒开、ESC/右键取消、<8px 视为取消、无文字区域「未识别到文字」淡出、设置改热键生效、与划词热键重复拦截、首次识别的模型加载等待体验
4. **打包**：`python -m PyInstaller tram.spec --noconfirm` → 检查 exe 体积（rapidocr 拖进 opencv-python，预期 ~120-160MB；仅记录实际值，超 200MB 才报警）→ 实机跑 exe 全流程（重点：**_MEIPASS 解包后模型可被找到**，这是打包最大风险点）
5. 推送分支后观察 CI 全绿

## 主要风险

| 风险 | 对策 |
|------|------|
| PyInstaller 解包后 RapidOCR 找不到模型（中） | 接线完成后**尽早**做打包实机验证；失败则改 `collect_data_files("rapidocr", includes=["models/**"])` + 显式模型路径 |
| collect_all 收集不全（中） | 打包后实测，按上条回退 |
| 首次 OCR init 数秒被误认为卡死（中） | popup「识别中…」已覆盖；可在托盘通知提示首次需加载模型 |
| onnxruntime 与 PyQt6 DLL 冲突（低） | 冒烟阶段两者同进程 import 即验证 |
| 旧 config `chi_sim+eng`（低） | `_LANG_ALIASES` 映射兜底 |

## 附：关键调研事实（2026-08-17）

- 本机 Python 3.14.5 + uv 0.12.1；项目 venv 为 3.14；CI/release workflow 用 Python 3.12
- paddlepaddle 3.3.1：cp313 wheel 有、**cp314 无**（GitHub issue #79527 open）→ 官方 paddleocr 路线需降级 3.13，已排除
- onnxruntime cp314 wheel 自 1.24.1 起提供（当前 1.28.0）；opencv-python 用 cp37-abi3 标签全版本兼容
- rapidocr 3.9.2：py3-none-any 26MB wheel，默认 PP-OCRv4 ch 模型（中英混排）内置；`pip install rapidocr onnxruntime`；用法 `engine = RapidOCR(); result = engine(png_bytes)`，结果在 `result.txts`
- 本机网络：PyPI / github.com / raw.githubusercontent 可达；winget 下载与 bcebos 等镜像不可达
- 磁盘 `vendor/tesseract/`（~80MB）为本机唯一可用 Tesseract 引擎，暂保留不删、不入库
