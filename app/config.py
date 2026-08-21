"""Tram 翻译软件 - 配置管理。

配置以 JSON 保存在用户主目录 ~/.tram/config.json。
使用 dataclass 提供类型安全，序列化为 JSON 持久化。
写入采用「临时文件 + 原子替换」，避免断电导致文件损坏。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "Tram Translator"

try:
    from importlib.metadata import version as _pkg_version

    APP_VERSION = _pkg_version("tram")
except Exception:  # 打包后元数据缺失时回退到包内常量
    from . import __version__

    APP_VERSION = __version__

logger = logging.getLogger(__name__)

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
    # 部分后端（如 Ryzen AI ONNX 服务）不支持 system 角色消息，
    # 带 system 的请求会返回 5xx。关闭后系统提示词并入用户消息发送。
    use_system_role: bool = True


@dataclass
class TranslationConfig:
    source_lang: str = "自动识别"  # 源语言，自动识别由模型判断
    target_lang: str = "中文（简体）"
    chunk_chars: int = 2000
    style: str = "忠实原文"
    custom_prompt: str = ""  # 自定义系统提示词，为空时使用默认模板


@dataclass
class SelectionConfig:
    enabled: bool = False  # 划词翻译模式开关
    hotkey: str = "Ctrl+F4"  # 全局热键：触发取词翻译
    min_chars: int = 2  # 选中文本短于此值跳过，避免误触发
    auto_hide_ms: int = 0  # 悬浮窗自动隐藏时间，0=失焦隐藏


@dataclass
class OCRConfig:
    enabled: bool = False  # OCR 识图翻译模式开关
    hotkey: str = "Ctrl+Shift+F4"  # 全局热键：触发框选识别
    languages: str = "ch"  # RapidOCR 语言码：ch = 中英混排（含纯英文）
    min_chars: int = 2  # 识别文本短于此值视为无文字，淡出提示


@dataclass
class MonitorConfig:
    enabled: bool = False  # 区域实时监控翻译模式开关（热键注册）
    hotkey: str = "Ctrl+Alt+F4"  # 全局热键：开始/停止监控（首次触发框选区域）
    interval_ms: int = 500  # 监控周期（毫秒）
    diff_threshold: float = 0.02  # 帧差比例超过此值视为画面变化（0~1）
    similarity_threshold: float = 0.88  # 文本相似度达到此值视为重复，不翻译
    debounce: int = 2  # 连续 N 个周期文本稳定才提交翻译（防渐入动画半截识别）
    history_size: int = 5  # 监控小窗保留最近 N 条翻译历史
    queue_size: int = 3  # 翻译忙时允许排队等待的字幕条数（满则丢最旧等待项）
    pause_on_cursor: bool = True  # 鼠标位于监控区域内时暂停识别（防悬停态误触发）
    min_chars: int = 2  # 识别文本短于此值视为无文字


@dataclass
class TramConfig:
    backend: BackendConfig = field(default_factory=BackendConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    ocr: OCRConfig = field(default_factory=OCRConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    glossary: list = field(default_factory=list)  # 运行时注入，不持久化


# ------------------------------------------------------------------ #
#  默认配置 dict（供 load_config 深拷贝使用）
# ------------------------------------------------------------------ #

DEFAULT_CONFIG: dict = asdict(TramConfig())
DEFAULT_CONFIG.pop("glossary", None)

# 运行时注入、不持久化到 config.json 的键。
# glossary 单独保存在 glossary.json，启动时由 main 注入 config；
# 若随 config 落盘会产生两份数据源，且磁盘上的副本很快过期。
_RUNTIME_ONLY_KEYS = frozenset({"glossary"})


def get_default(section: str, key: str):
    """读取某个配置项的默认值。

    dataclass 字段默认是唯一事实来源：消费者代码禁止再手写字面量
    默认值（如 "Ctrl+F4"、2000），改用 get_default("selection", "hotkey")，
    避免默认值散落多处后改一处漏一处。
    """
    return DEFAULT_CONFIG[section][key]

# 已知数值/布尔字段的类型强制转换表，防止手动编辑 config.json
# 时写入错误类型导致后续运行时崩溃
_TYPE_COERCIONS: dict[str, dict[str, type]] = {
    "backend": {
        "temperature": float,
        "max_tokens": int,
        "timeout": int,
        "use_system_role": bool,
    },
    "translation": {
        "chunk_chars": int,
    },
    "selection": {
        "enabled": bool,
        "min_chars": int,
        "auto_hide_ms": int,
    },
    "ocr": {
        "enabled": bool,
        "min_chars": int,
    },
    "monitor": {
        "enabled": bool,
        "interval_ms": int,
        "diff_threshold": float,
        "similarity_threshold": float,
        "debounce": int,
        "history_size": int,
        "queue_size": int,
        "pause_on_cursor": bool,
        "min_chars": int,
    },
}


def _coerce_types(cfg: dict) -> dict:
    """对已知的数值/布尔字段做类型强制转换。

    手动编辑 config.json 可能写入字符串类型的数值（如 "chunk_chars": "2000"），
    不转换会在后续 int()/float() 调用时崩溃。此处统一兜底。
    """
    for section, fields in _TYPE_COERCIONS.items():
        if section not in cfg or not isinstance(cfg[section], dict):
            continue
        for key, typ in fields.items():
            val = cfg[section].get(key)
            if val is None:
                # 显式 null 视为缺失，回退默认值（这些字段默认值均非 None）
                cfg[section][key] = DEFAULT_CONFIG.get(section, {}).get(key)
                continue
            try:
                if typ is bool:
                    # bool("false") == True，需特殊处理
                    if isinstance(val, str):
                        cfg[section][key] = val.strip().lower() in ("true", "1", "yes")
                    else:
                        cfg[section][key] = bool(val)
                elif typ is int:
                    cfg[section][key] = int(val)
                elif typ is float:
                    cfg[section][key] = float(val)
            except (ValueError, TypeError):
                logger.warning(
                    "配置项 %s.%s 值 %r 类型转换失败，保留默认", section, key, val
                )
                cfg[section][key] = DEFAULT_CONFIG.get(section, {}).get(key)
    return cfg


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
    """读取配置为 dict（兼容旧接口），缺失项用默认值补齐。

    历史版本的 save_config 曾把运行时键（如 glossary）写进
    config.json，读取时直接忽略这些键，以文件系统中的专用存储为准。
    """
    cfg: dict = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                for section, values in stored.items():
                    if section in _RUNTIME_ONLY_KEYS:
                        continue
                    if isinstance(values, dict):
                        cfg.setdefault(section, {}).update(values)
                    else:
                        # 非 dict 的节（如手动把 "backend" 改成了字符串）
                        # 直接忽略并保留默认值，否则后续 .get().get() 链
                        # 会以 AttributeError 崩溃
                        logger.warning(
                            "配置节 %s 非 dict（%s），忽略并使用默认值",
                            section, type(values).__name__,
                        )
    except (json.JSONDecodeError, OSError):
        pass  # 配置损坏时静默回退到默认
    return _coerce_types(cfg)


def save_config(cfg: dict) -> None:
    """原子写入配置到 JSON 文件。

    剥离运行时注入的键（glossary 等），它们有自己的持久化位置，
    不应混入 config.json。
    """
    data = {k: v for k, v in cfg.items() if k not in _RUNTIME_ONLY_KEYS}
    _atomic_write(CONFIG_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def save_glossary_json(entries: list[dict]) -> None:
    """原子写入术语表到 JSON 文件。"""
    _atomic_write(
        GLOSSARY_FILE, json.dumps(entries, ensure_ascii=False, indent=2)
    )
