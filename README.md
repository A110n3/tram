# Tram — 离线划词翻译

接入本地大模型（Ollama / LM Studio / vLLM 等任意 OpenAI 兼容后端）的**纯离线**划词翻译工具。

选中文本 → 按热键 → 悬浮窗流式显示译文。全程本地运行，不联网、不上传。

## 功能

- **全局热键取词**：选中任意文本，按热键（默认 `Ctrl+Shift+T`），自动获取并翻译
- **流式悬浮窗**：译文边生成边显示，跟随鼠标，失焦自动隐藏，可拖动
- **系统托盘常驻**：后台静默运行，关闭窗口自动最小化到托盘
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

```bash
pip install -r requirements.txt
python -m app.main
```

首次运行请在托盘菜单中打开「设置」，选择后端预设并填写模型名称（如 `qwen2.5:7b`），点击「测试连接」确认可用。然后勾选「启用划词翻译」。

## 使用说明

| 操作 | 方式 |
|------|------|
| 开启/关闭划词 | 右键托盘图标 → 划词翻译 |
| 取词翻译 | 选中文本后按 `Ctrl+Shift+T`（可在设置中修改） |
| 关闭悬浮窗 | 点击 ✕ 或点击窗口外部 |
| 拖动悬浮窗 | 按住任意空白区域拖动 |
| 切换模型 | 托盘菜单 → 设置 |
| 退出程序 | 托盘菜单 → 退出 |

## 运行测试

```bash
python tests/test_core.py
```

## 项目结构

```
app/
├── main.py                     # 入口
├── config.py                   # 配置持久化 (~/.tram/config.json)
├── core/
│   ├── backend.py              # OpenAI 兼容流式客户端
│   ├── chunking.py             # 长文本分段
│   ├── glossary.py             # 术语表 (~/.tram/glossary.json)
│   ├── hotkey.py               # 全局热键监听 (Win32 RegisterHotKey)
│   ├── prompts.py              # 翻译提示词模板
│   ├── selection.py            # 模拟 Ctrl+C 取词 + 剪贴板恢复
│   └── translator.py           # 翻译编排
└── ui/
    ├── main_window.py          # 托盘常驻主窗口
    ├── popup.py                # 悬浮窗
    ├── selection_translator.py # 划词翻译编排器
    ├── settings_dialog.py      # 设置对话框
    ├── glossary_dialog.py      # 术语表编辑
    └── worker.py               # 翻译后台线程
```

## 路线图

- [x] 后台划词翻译（全局热键 + 悬浮窗 + 托盘常驻）
- [x] 后端配置 + 连接测试
- [x] 术语表 + 上下文保持
- [x] 多后端可切换
- [ ] 文件翻译（txt / markdown / SRT）
- [ ] 历史记录
- [ ] 双语导出
