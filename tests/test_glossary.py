"""术语表测试。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import app.core.glossary as gs
from app.core.glossary import load_glossary, to_prompt_block


def test_load_glossary_malformed_entries():
    """损坏的条目（非 dict）不崩溃，跳过。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(["hello", 123, None, {"source": "ok", "target": "好"}], tmp)
        path = tmp.name

    orig = gs.GLOSSARY_FILE
    gs.GLOSSARY_FILE = Path(path)
    try:
        result = load_glossary()
        assert result == [{"source": "ok", "target": "好"}]
    finally:
        gs.GLOSSARY_FILE = orig
        os.unlink(path)


def test_load_glossary_missing_file():
    """文件不存在时返回空列表。"""
    orig = gs.GLOSSARY_FILE
    gs.GLOSSARY_FILE = Path("/nonexistent/path/glossary.json")
    try:
        assert load_glossary() == []
    finally:
        gs.GLOSSARY_FILE = orig


def test_load_glossary_corrupted_json():
    """损坏的 JSON 返回空列表。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write("not valid json {{{")
        path = tmp.name

    orig = gs.GLOSSARY_FILE
    gs.GLOSSARY_FILE = Path(path)
    try:
        assert load_glossary() == []
    finally:
        gs.GLOSSARY_FILE = orig
        os.unlink(path)


def test_load_glossary_skips_empty_fields():
    """source 或 target 为空的条目被跳过。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump([
            {"source": "", "target": "x"},
            {"source": "x", "target": ""},
            {"source": "ok", "target": "好"},
        ], tmp)
        path = tmp.name

    orig = gs.GLOSSARY_FILE
    gs.GLOSSARY_FILE = Path(path)
    try:
        result = load_glossary()
        assert len(result) == 1
        assert result[0]["source"] == "ok"
    finally:
        gs.GLOSSARY_FILE = orig
        os.unlink(path)


def test_to_prompt_block_empty():
    assert to_prompt_block([]) == ""


def test_to_prompt_block_format():
    entries = [{"source": "API", "target": "接口"}]
    block = to_prompt_block(entries)
    assert "API => 接口" in block
    assert "术语表" in block
