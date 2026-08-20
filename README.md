# Tram — 离线划词/识图翻译

买了一个带NPU的笔记本电脑，发现基本没什么用。为了给我花钱买的NPU找事做，我搓了这个可以接入本地模型的离线翻译软件，分享给有需要的朋友。

接入本地大模型（Ollama / LM Studio / vLLM 等任意 OpenAI 兼容后端）的**纯离线**翻译工具：划词翻译 + OCR 识图翻译 + 区域实时监控。

选中文本（或框选屏幕区域）→ 按热键 → 悬浮窗流式显示译文。全程本地运行，不联网、不上传。

## 功能

- **全局热键取词**：选中任意文本，按热键（默认 `Ctrl+F4`），自动获取并翻译
- **OCR 识图翻译**：按热键（默认 `Ctrl+Shift+F4`）框选屏幕任意区域，内置中英文 OCR 模型识别后翻译（图片、PDF、视频字幕皆可）
- **区域实时监控**：按热键（默认 `Ctrl+Alt+F4`）框选字幕区域，持续监控画面变化，出现新字幕自动识别翻译，独立置顶小窗流式显示译文 + 最近历史
- **流式悬浮窗**：译文边生成边显示，跟随鼠标，失焦自动隐藏，可拖动
- **系统托盘常驻**：后台静默运行，自动最小化到托盘
- **多后端切换**：Ollama / LM Studio / vLLM 等任意 OpenAI 兼容 API
- **术语表**：自定义术语映射，确保专有名词翻译准确
- **可配置**：热键、后端、模型、温度等均可自定义

## 环境要求

- Windows 10+
- Python 3.10+
- 本机已安装并运行任一 OpenAI 兼容推理后端：
  - [Ollama](https://ollama.com/)（默认 `http://localhost:11434/v1`）
  - [LM Studio](https://lmstudio.ai/)（`http://localhost:1234/v1`）
  - vLLM 或其他（自定义 Base URL）

## 安装与运行

**方式一：exe 免安装**（推荐）

从 [Releases](../../releases) 下载 `Tram.exe`，双击运行。

**方式二：源码运行**

```bash
pip install .
python -m app.main
```

OCR 识图翻译为可选功能，引擎栈（rapidocr + onnxruntime，含内置中英文模型，约 150MB）不随主程序安装：

```bash
pip install ".[ocr]"
```

首次运行请在托盘菜单中打开「设置」，选择后端预设并填写模型名称（如 `qwen2.5:7b`），点击「测试连接」确认可用。然后勾选「启用划词翻译」。

运行日志位于 `~/.tram/tram.log`，反馈问题时请附带。

## 使用说明

| 操作 | 方式 |
|------|------|
| 开启/关闭划词 | 右键托盘图标 → 划词翻译 |
| 取词翻译 | 选中文本后按 `Ctrl+F4`（可在设置中修改） |
| 开启/关闭 OCR | 右键托盘图标 → OCR 识图翻译 |
| 识图翻译 | 按 `Ctrl+Shift+F4` 框选屏幕区域（可在设置中修改） |
| 开启/关闭监控 | 右键托盘图标 → 区域实时监控 |
| 实时字幕翻译 | 按 `Ctrl+Alt+F4` 框选字幕区域开始监控，再按一次停止（可在设置中修改） |
| 关闭悬浮窗 | 点击 ✕ 或点击窗口外部 |
| 拖动悬浮窗 | 按住任意空白区域拖动 |
| 切换目标语言 | 托盘菜单 → 目标语言（切换后自动测试连接并通知结果） |
| 切换模型 | 托盘菜单 → 设置 |
| 退出程序 | 托盘菜单 → 退出 |

OCR 说明：框选后可用 ESC/右键取消；首次识别需加载模型（数秒），之后秒开；
未安装 OCR 可选依赖时，按热键会在托盘提示安装命令。

监控说明：监控采用「帧差门控 → OCR → 文本查重防抖」漏斗，只有出现新字幕才触发翻译，
静态画面几乎零开销；字幕来回闪（A→B→A）不会重复翻译；翻译丢旧保新，始终显示最新字幕；
关闭监控小窗即停止监控。监控间隔、帧差阈值、相似度阈值等可在设置中调整。

## 开发与测试

```bash
pip install ".[dev,ocr]"

python -m pytest tests/ -q      # 运行测试
python -m ruff check app/ tests/ # 代码检查
python -m mypy app/              # 类型检查
build.bat                        # 打包 dist/Tram.exe
```

## 项目结构

```
app/
├── main.py                     # 入口
├── config.py                   # 配置持久化 (~/.tram/config.json)，原子写入
├── logging_config.py           # 日志 (~/.tram/tram.log)，滚动备份
├── core/
│   ├── backend.py              # OpenAI 兼容流式客户端
│   ├── chunking.py             # 长文本分段
│   ├── glossary.py             # 术语表 (~/.tram/glossary.json)
│   ├── hotkey.py               # 全局热键监听 (Win32 RegisterHotKey)
│   ├── monitor.py              # 区域监控漏斗（帧差门控/预处理/查重防抖，纯逻辑可单测）
│   ├── ocr.py                  # RapidOCR 封装（PaddleOCR 模型 ONNX 版）
│   ├── prompts.py              # 翻译提示词模板
│   ├── selection.py            # 模拟 Ctrl+C 取词 + 剪贴板恢复
│   └── translator.py           # 翻译编排
└── ui/
    ├── main_window.py          # 托盘常驻主窗口（含「关于」页）
    ├── base_translator.py      # 热键翻译编排器公共骨架（后端/热键生命周期、去重缓存、取消）
    ├── selection_translator.py # 划词翻译编排器
    ├── ocr_translator.py       # OCR 识图翻译编排器
    ├── monitor_translator.py   # 区域实时监控编排器（框选 → 监控 → 丢旧保新翻译）
    ├── monitor_window.py       # 监控置顶小窗（最新译文 + 历史）
    ├── popup.py                # 悬浮窗
    ├── region_overlay.py       # 全屏框选覆盖层
    ├── settings_dialog.py      # 设置对话框
    ├── glossary_dialog.py      # 术语表编辑
    ├── worker.py               # 翻译/OCR/连接测试后台线程
    └── worker_util.py          # 僵尸 QThread 包装器免疫
```

## 路线图

- [x] 后台划词翻译（全局热键 + 悬浮窗 + 托盘常驻）
- [x] 后端配置 + 连接测试
- [x] 术语表 + 上下文保持
- [x] 多后端可切换
- [x] OCR 识图翻译（RapidOCR 内置中英文模型，框选即译）
- [x] 区域实时监控翻译（帧差门控漏斗 + 独立小窗，实时字幕）
- [ ] OCR 更多语言（日/韩等专用模型）
- [ ] 文件翻译（txt / markdown / SRT）
- [ ] 历史记录
- [ ] 双语导出
