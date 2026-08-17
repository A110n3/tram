"""配置管理测试。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import app.config as cfg
from app.config import TramConfig, load_config, save_config


def test_tram_config_defaults():
    c = TramConfig()
    assert c.backend.base_url == "http://localhost:11434/v1"
    assert c.translation.chunk_chars == 2000
    assert c.selection.enabled is False
    assert c.ocr.enabled is False
    assert c.ocr.hotkey == "Ctrl+Shift+F4"
    assert c.ocr.languages == "ch"
    assert c.ocr.min_chars == 2


def test_load_config_ocr_section_and_coercion():
    """ocr 段读取 + 数值/布尔类型强制转换。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump({
            "ocr": {
                "enabled": "true",
                "hotkey": "Ctrl+Shift+F2",
                "languages": "jpn+eng",
                "min_chars": "3",
            },
        }, tmp)
        path = tmp.name

    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path(path)
    try:
        result = load_config()
        assert result["ocr"]["enabled"] is True
        assert result["ocr"]["hotkey"] == "Ctrl+Shift+F2"
        assert result["ocr"]["languages"] == "jpn+eng"
        assert result["ocr"]["min_chars"] == 3
        assert isinstance(result["ocr"]["min_chars"], int)
    finally:
        cfg.CONFIG_FILE = orig
        os.unlink(path)


def test_load_config_ocr_missing_uses_defaults():
    """旧版 config.json 无 ocr 段时补齐默认值。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump({"selection": {"enabled": True}}, tmp)
        path = tmp.name

    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path(path)
    try:
        result = load_config()
        assert result["ocr"] == {
            "enabled": False,
            "hotkey": "Ctrl+Shift+F4",
            "languages": "ch",
            "min_chars": 2,
        }
    finally:
        cfg.CONFIG_FILE = orig
        os.unlink(path)


def test_load_config_missing_file():
    """配置文件不存在时返回默认值。"""
    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path("/nonexistent/config.json")
    try:
        result = load_config()
        assert result["backend"]["base_url"] == "http://localhost:11434/v1"
        assert result["selection"]["enabled"] is False
    finally:
        cfg.CONFIG_FILE = orig


def test_load_config_corrupted():
    """损坏的 JSON 回退默认值。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write("corrupted json {{{")
        path = tmp.name

    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path(path)
    try:
        result = load_config()
        assert result["backend"]["model"] == "qwen2.5:7b"
    finally:
        cfg.CONFIG_FILE = orig
        os.unlink(path)


def test_save_and_load_roundtrip(tmp_path):
    """保存后重新加载，值一致。"""
    orig_dir = cfg.CONFIG_DIR
    orig_file = cfg.CONFIG_FILE
    cfg.CONFIG_DIR = tmp_path
    cfg.CONFIG_FILE = tmp_path / "config.json"
    try:
        config = load_config()
        config["backend"]["model"] = "llama3.1:8b"
        config["selection"]["enabled"] = True
        save_config(config)

        loaded = load_config()
        assert loaded["backend"]["model"] == "llama3.1:8b"
        assert loaded["selection"]["enabled"] is True
    finally:
        cfg.CONFIG_DIR = orig_dir
        cfg.CONFIG_FILE = orig_file


def test_atomic_write_no_partial_file(tmp_path):
    """原子写入：即使覆盖已有文件，也不会出现部分写入。"""
    orig_dir = cfg.CONFIG_DIR
    orig_file = cfg.CONFIG_FILE
    cfg.CONFIG_DIR = tmp_path
    cfg.CONFIG_FILE = tmp_path / "config.json"
    try:
        # 先写一个有效配置
        save_config(load_config())
        assert cfg.CONFIG_FILE.exists()

        # 再写一次，文件应完整可解析
        config2 = load_config()
        config2["backend"]["model"] = "qwen3:32b"
        save_config(config2)

        with open(cfg.CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        assert data["backend"]["model"] == "qwen3:32b"
    finally:
        cfg.CONFIG_DIR = orig_dir
        cfg.CONFIG_FILE = orig_file


def test_load_config_coerces_string_numbers():
    """手动编辑写入字符串类型的数值字段时，load_config 强制转换。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump({
            "backend": {
                "temperature": "0.5",
                "max_tokens": "4096",
                "timeout": "120",
                "use_system_role": "false",
            },
            "translation": {
                "chunk_chars": "512",
            },
            "selection": {
                "enabled": "true",
                "min_chars": "3",
                "auto_hide_ms": "5000",
            },
        }, tmp)
        path = tmp.name

    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path(path)
    try:
        result = load_config()
        assert result["backend"]["temperature"] == 0.5
        assert isinstance(result["backend"]["temperature"], float)
        assert result["backend"]["max_tokens"] == 4096
        assert isinstance(result["backend"]["max_tokens"], int)
        assert result["backend"]["timeout"] == 120
        assert result["backend"]["use_system_role"] is False
        assert result["translation"]["chunk_chars"] == 512
        assert result["selection"]["enabled"] is True
        assert result["selection"]["min_chars"] == 3
        assert result["selection"]["auto_hide_ms"] == 5000
    finally:
        cfg.CONFIG_FILE = orig
        os.unlink(path)


def test_load_config_coerces_bad_values_fallback():
    """无法转换的值回退到默认，不崩溃。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump({
            "backend": {
                "temperature": "not_a_number",
                "max_tokens": None,
            },
            "translation": {
                "chunk_chars": [1, 2, 3],
            },
        }, tmp)
        path = tmp.name

    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path(path)
    try:
        result = load_config()
        # 回退到默认值
        assert result["backend"]["temperature"] == 0.2
        assert result["backend"]["max_tokens"] == 2048
        assert result["translation"]["chunk_chars"] == 2000
    finally:
        cfg.CONFIG_FILE = orig
        os.unlink(path)


def test_save_config_strips_runtime_glossary(tmp_path):
    """glossary 是运行时注入的键，不得随 config.json 持久化。

    术语表有自己的持久化文件（glossary.json）。若混入 config.json
    会产生两份数据源，磁盘上的副本随过期配置误导排查。
    """
    orig_dir = cfg.CONFIG_DIR
    orig_file = cfg.CONFIG_FILE
    cfg.CONFIG_DIR = tmp_path
    cfg.CONFIG_FILE = tmp_path / "config.json"
    try:
        config = load_config()
        config["glossary"] = [{"source": "API", "target": "接口"}]
        save_config(config)

        with open(cfg.CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        assert "glossary" not in data
        # 正常配置不受影响
        assert data["backend"]["base_url"] == "http://localhost:11434/v1"
    finally:
        cfg.CONFIG_DIR = orig_dir
        cfg.CONFIG_FILE = orig_file


def test_load_config_ignores_stored_glossary():
    """旧版写入 config.json 的残留 glossary 键，读取时忽略。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump({
            "backend": {"model": "m1"},
            "glossary": [{"source": "x", "target": "y"}],
        }, tmp)
        path = tmp.name

    orig = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = Path(path)
    try:
        result = load_config()
        assert "glossary" not in result
        # 其余配置正常读取
        assert result["backend"]["model"] == "m1"
    finally:
        cfg.CONFIG_FILE = orig
        os.unlink(path)
