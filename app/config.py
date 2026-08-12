"""Tram 翻译软件 - 配置管理

配置以 JSON 保存在用户主目录 ~/.tram/config.json，
允许通过设置界面修改并持久化。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "Tram Translator"
APP_VERSION = "0.1.0b1"

CONFIG_DIR = Path(os.path.expanduser("~")) / ".tram"
CONFIG_FILE = CONFIG_DIR / "config.json"
GLOSSARY_FILE = CONFIG_DIR / "glossary.json"

DEFAULT_CONFIG: dict = {
    "backend": {
        "base_url": "http://localhost:11434/v1",  # Ollama；LM Studio 为 http://localhost:1234/v1
        "api_key": "ollama",  # 本地后端通常可填任意值
        "model": "qwen2.5:7b",
        "temperature": 0.2,
        "max_tokens": 2048,
        "timeout": 180,
    },
    "translation": {
        "source_lang": "auto",
        "target_lang": "中文（简体）",
        "chunk_chars": 2000,
        "style": "忠实原文",
    },
}


def ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """读取配置，缺失项用默认值补齐。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                stored = json.load(f)
            for section, values in stored.items():
                if isinstance(values, dict):
                    cfg.setdefault(section, {}).update(values)
                else:
                    cfg[section] = values
    except (json.JSONDecodeError, OSError):
        pass  # 配置损坏时静默回退到默认
    return cfg


def save_config(cfg: dict) -> None:
    ensure_dir()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
