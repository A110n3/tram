"""Tram 翻译软件 - 配置管理。

配置以 JSON 保存在用户主目录 ~/.tram/config.json。
使用 dataclass 提供类型安全，序列化为 JSON 持久化。
写入采用「临时文件 + 原子替换」，避免断电导致文件损坏。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

APP_NAME = "Tram Translator"
APP_VERSION = "0.2.1"

CONFIG_DIR = Path(os.path.expanduser("~")) / ".tram"
CONFIG_FILE = CONFIG_DIR / "config.json"
GLOSSARY_FILE = CONFIG_DIR / "glossary.json"


# ------------------------------------------------------------------ #
#  dataclass 配置模型
# ------------------------------------------------------------------ #


@dataclass
class BackendConfig:
    base_url: str = "http://localhost:11434/v1"  # Ollama；LM Studio 为 http://localhost:1234/v1
    api_key: str = "ollama"  # 本地后端通常可填任意值
    model: str = "qwen2.5:7b"
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: int = 180


@dataclass
class TranslationConfig:
    source_lang: str = "auto"
    target_lang: str = "中文（简体）"
    chunk_chars: int = 2000
    style: str = "忠实原文"


@dataclass
class SelectionConfig:
    enabled: bool = False  # 划词翻译模式开关
    hotkey: str = "Ctrl+F4"  # 全局热键：触发取词翻译
    min_chars: int = 2  # 选中文本短于此值跳过，避免误触发
    auto_hide_ms: int = 0  # 悬浮窗自动隐藏时间，0=失焦隐藏


@dataclass
class TramConfig:
    backend: BackendConfig = field(default_factory=BackendConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    glossary: list = field(default_factory=list)  # 运行时注入，不持久化

    @classmethod
    def from_dict(cls, data: dict) -> TramConfig:
        """从嵌套 dict 构造，忽略未知字段，缺失项用默认值。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        for f in fields(cls):
            val = data.get(f.name)
            if val is None:
                continue
            if is_dataclass(f.type) if isinstance(f.type, type) else False:
                sub = getattr(cfg, f.name)
                if isinstance(val, dict):
                    for k, v in val.items():
                        if hasattr(sub, k):
                            setattr(sub, k, v)
            else:
                setattr(cfg, f.name, val)
        return cfg

    def to_persist_dict(self) -> dict:
        """转为可持久化的 dict（排除 glossary 等运行时字段）。"""
        d = asdict(self)
        d.pop("glossary", None)
        return d


# ------------------------------------------------------------------ #
#  兼容层：提供 DEFAULT_CONFIG 供旧代码引用
# ------------------------------------------------------------------ #

DEFAULT_CONFIG: dict = asdict(TramConfig())
DEFAULT_CONFIG.pop("glossary", None)


# ------------------------------------------------------------------ #
#  持久化
# ------------------------------------------------------------------ #


def ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, data: str) -> None:
    """原子写入：先写临时文件，再 os.replace 原子替换。"""
    ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_config() -> dict:
    """读取配置为 dict（兼容旧接口），缺失项用默认值补齐。"""
    cfg: dict = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                for section, values in stored.items():
                    if isinstance(values, dict):
                        cfg.setdefault(section, {}).update(values)
                    else:
                        cfg[section] = values
    except (json.JSONDecodeError, OSError):
        pass  # 配置损坏时静默回退到默认
    return cfg


def save_config(cfg: dict) -> None:
    """原子写入配置到 JSON 文件。"""
    _atomic_write(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2))


def save_glossary_json(entries: list[dict]) -> None:
    """原子写入术语表到 JSON 文件。"""
    _atomic_write(
        GLOSSARY_FILE, json.dumps(entries, ensure_ascii=False, indent=2)
    )
