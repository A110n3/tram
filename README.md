# Tram 离线翻译

接入本地大模型（Ollama / LM Studio / vLLM 等任意 OpenAI 兼容后端）的**纯离线**翻译桌面应用。

- 隐私：所有文本只发往本机 localhost，绝不上传云端
- 流式输出：边翻译边显示
- 长文本自动分段，跨块保持上下文与术语一致
- 术语表：指定术语必须采用指定译文
- 可随时停止翻译

## 环境要求

- Python 3.10+
- 本机已安装并运行任一 OpenAI 兼容推理后端：
  - [Ollama](https://ollama.com/)（默认，`http://localhost:11434/v1`）
  - [LM Studio](https://lmstudio.ai/)（`http://localhost:1234/v1`）
  - vLLM 或其他（自定义 Base URL）

## 安装与运行

```bash
pip install -r requirements.txt
python -m app.main
```

首次运行请打开「工具 → 设置」，选择后端预设、填写模型名称（如 `qwen2.5:7b`），点击「测试连接」确认可用。

## 运行测试

```bash
python tests/test_core.py
```

## 项目结构

```
app/
├── main.py            # 入口
├── config.py          # 配置持久化 (~/.tram/config.json)
├── core/
│   ├── backend.py     # OpenAI 兼容流式客户端
│   ├── chunking.py    # 长文本分段
│   ├── prompts.py     # 翻译提示词模板
│   ├── glossary.py    # 术语表 (~/.tram/glossary.json)
│   └── translator.py  # 翻译编排（分段→上下文→流式）
└── ui/
    ├── main_window.py # 双栏主窗口
    ├── settings_dialog.py
    └── glossary_dialog.py
```

## 路线图

- [x] 输入/粘贴翻译（双栏对照、流式）
- [x] 后端配置 + 连接测试
- [x] 术语表 + 上下文保持
- [ ] 文件翻译（txt / markdown / SRT）
- [ ] 剪贴板监听划词翻译
- [ ] 历史记录、双语导出
