# OCR 识图翻译实现方案（定稿，2026-08-14）

引擎：**Tesseract**（tessdata_fast 档），经体积/多语言/打包三方面实测评估后确定。
目标版本：**v0.3.0**。

## 一、总体流程

```
按 OCR 热键（默认 Ctrl+Shift+F4）
  → 截取主屏幕（在显示覆盖层之前截，避免把遮罩截进去）
  → 全屏选区覆盖层（画面冻结 + 变暗，拖拽框选，ESC/右键取消）
  → 裁剪图像 → 预处理（选区过小时放大）
  → OCRWorker（QThread，子进程调 tesseract.exe）→ 得到文本
  → 复用现有 TranslateWorker 流式翻译 → TranslationPopup 展示
```

OCR 产出的文本直接喂给现有 `Translator` + `TranslateWorker` + `TranslationPopup`，
与划词翻译共用整条翻译管线（loading/流式/错误/缓存重显全部复用）。

## 二、新增文件

| 文件 | 职责 |
|---|---|
| `app/core/ocr.py` | Tesseract 封装：二进制定位、子进程调用、图像预处理、输出清洗。自写 ~50 行子进程封装，**不引 pytesseract/Pillow** |
| `app/ui/region_overlay.py` | 全屏选区覆盖层（QWidget） |
| `app/ui/ocr_translator.py` | `OCRTranslator(QObject)` 编排器，镜像 `SelectionTranslator` 结构 |
| `app/ui/worker.py` 内新增 | `OCRWorker(QThread)`：跑 tesseract 子进程，信号 `succeeded(str)/failed(str)` |
| `tools/fetch_tesseract.py` | 拉取 tesseract.exe + 语言包到 `vendor/tesseract/` |

## 三、修改文件

| 文件 | 改动 |
|---|---|
| `app/config.py` | 新增 `OCRConfig` dataclass + `_TYPE_COERCIONS` 补 ocr 段 |
| `app/ui/main_window.py` | 托盘菜单加「OCR 识图翻译」勾选项；OCRTranslator 生命周期；设置保存后重建 |
| `app/ui/settings_dialog.py` | 新增「OCR 识图」分组：启用开关 + 热键（复用 `test_hotkey_available` 校验，补查与划词热键重复） |
| `tram.spec` | `datas` 加入 `vendor/tesseract` |
| `.github/workflows/release.yml` | 打包前准备 Tesseract（安装 → 拷贝 exe + 语言包到 vendor/） |
| `README.md` | 路线图勾选 OCR + 使用说明 |

**不新增 Python 依赖**：pyproject.toml dependencies 不变。

## 四、核心模块设计

### app/core/ocr.py

```python
def find_tesseract() -> Path | None
# 查找顺序：① 打包内置 _MEIPASS/vendor/tesseract/tesseract.exe
#          ② 开发环境仓库内 vendor/tesseract/
#          ③ PATH 中的 tesseract
#          ④ 常见安装目录 %ProgramFiles%\Tesseract-OCR

def ocr_image(pixmap: QPixmap, languages: str) -> str
# 预处理 → 存临时 PNG → subprocess 调用 → 解码 UTF-8 → 清洗返回
# 参数：--psm 3（全自动分割），超时 10s，失败抛 OCRError
```

- 预处理（v1 从简）：选区高度 < 60px 时放大 2~3 倍；不做灰度/二值化
- 输出清洗：去首尾空白、去掉末尾空行，保留行间换行

### app/ui/region_overlay.py

- 全屏置顶无边框窗口，背景 = **冻结的截图 + 半透明黑遮罩**
- 拖拽时：框内还原原始亮度 + 边框高亮，十字光标
- 鼠标释放 → `region_selected(QRect)`；框 < 8×8 视为取消
- ESC / 右键 → `cancelled` 信号
- HiDPI：裁剪坐标乘 `devicePixelRatio`；v1 只截主屏（多屏留 v2）

### app/ui/ocr_translator.py

结构与 `SelectionTranslator` 一一对应（独立实例、独立热键线程
`GlobalHotkeyThread(hotkey, hotkey_id=2)`）：

```
_on_hotkey:  取消旧 worker/popup → 截屏 → 弹覆盖层
_on_region:  裁剪 → popup.show_loading("识别中…") → OCRWorker
_on_ocr_ok:  文本为空 → "未识别到文字"淡出
             去重命中 → show_cached 秒开
             否则 → TranslateWorker 流式翻译
_on_ocr_err: popup.show_error
```

去重缓存沿用 `_last_text/_last_result` 模式，受 `invalidate_last_text()` 联动。

## 五、配置

```python
@dataclass
class OCRConfig:
    enabled: bool = False
    hotkey: str = "Ctrl+Shift+F4"
    languages: str = "chi_sim+eng"   # 正式字段；v1 只在 config.json 手改，
                                     # 预留将来设置界面「识别语言」下拉框
    min_chars: int = 2
```

注意：**打包进哪些语言** ≠ **一次识别用哪些**。`-l` 混排语言越多精度越掉，
默认 `chi_sim+eng`，需要日文等时手改此字段。

## 六、Tesseract 供给

### 语言包（tessdata_fast，实测体积）

| 文件 | 体积 |
|---|---|
| chi_sim.traineddata | 2.35 MB |
| chi_tra.traineddata | 2.26 MB |
| jpn.traineddata | 2.36 MB |
| eng.traineddata | 3.92 MB |
| kor.traineddata | 1.60 MB |
| rus.traineddata | 3.68 MB |
| **合计** | **16.2 MB** |

**六种语言全部打包**，开箱覆盖中/繁/日/英/韩/俄。加引擎（~10MB）总增量 ~26MB。
不用标准版（体积 6~18 倍，fast 对清晰截图足够）。

### 存放与获取

```
vendor/tesseract/          ← .gitignore 排除，不入库
  ├── tesseract.exe        （UB Mannheim 构建）
  └── tessdata/{chi_sim,chi_tra,jpn,eng,kor,rus}.traineddata
```

- **开发**：`winget install UB-Mannheim.TesseractOCR`（③④ 路径自动发现），
  或跑 `tools/fetch_tesseract.py` 拉全六语言到 vendor/
- **CI 发布**：release.yml 打包前安装 Tesseract 并拷贝进 vendor/
- 运行时发现失败：托盘通知引导用户检查安装

## 七、错误处理

- Tesseract 未找到 → 启用时/首次触发时托盘通知引导
- OCR 空结果 → 浮窗"未识别到文字"淡出
- 子进程崩溃/超时（10s）→ popup.show_error
- stdout 按 UTF-8 解码；临时 PNG 用 tempfile（Tesseract 5.x 支持 Unicode 路径）

## 八、测试与验证

- `tests/test_ocr.py`：参数构造、输出清洗、二进制定位逻辑（mock subprocess）
- `tests/test_config.py` 补 OCRConfig 用例
- 运行时验证（项目铁律：Win32/热键改动必须实跑）：
  热键 → 覆盖层 → 中英文截图各测一次 → 改配置测日文 → 重建 exe 再测

## 九、明确不做（v2+）

多显示器截屏、"仅 OCR 不翻译"模式、表格/版面分析、Vision LLM 混合模式、
设置界面「识别语言」下拉框。

## 十、实施顺序

1. `app/config.py` OCRConfig + 测试
2. `app/core/ocr.py` + `tools/fetch_tesseract.py` + 测试（无 UI 依赖）
3. `app/ui/region_overlay.py`
4. `OCRWorker` + `app/ui/ocr_translator.py`
5. `main_window.py` / `settings_dialog.py` 接线
6. `tram.spec` / `release.yml` / README / 版本号
7. 运行时验证 + 重建 exe
