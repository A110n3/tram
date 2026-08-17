"""拉取 Tesseract 引擎与语言包到 vendor/tesseract/（开发环境用）。

引擎：从本机已安装的 Tesseract（winget/choco/UB Mannheim 安装器）
复制 tesseract.exe + 运行所需 DLL 到 vendor/tesseract/。
vendor/ 已有引擎而未安装时跳过引擎复制，仅补语言包。
未安装且 vendor/ 为空时提示先执行: winget install UB-Mannheim.TesseractOCR

瘦身：UB-Mannheim 5.4.0 安装包内嵌 DWARF 调试节区（libtesseract-5.dll
约 100MB），复制到 vendor/ 后自动剥离调试节区（等价 strip --strip-debug），
恢复到 ~3MB。需要 pefile（dev 依赖）；未安装时跳过不报错。

语言包：从 tessdata_fast 仓库下载六种语言（中简/中繁/日/英/韩/俄），
fast 档对清晰截图足够，体积仅为标准版的 1/6~1/18。

用法: python tools/fetch_tesseract.py [--langs chi_sim,chi_tra,jpn,eng,kor,rus]
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "vendor" / "tesseract"
DEFAULT_LANGS = ["chi_sim", "chi_tra", "jpn", "eng", "kor", "rus"]
TESSDATA_FAST_BASE = (
    "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0"
)

# DLL 超过此体积才尝试剥离调试节区（正常 libtesseract ~3-5MB）
_STRIP_THRESHOLD = 10 * 1024 * 1024


def find_installed_tesseract() -> Path | None:
    """定位本机已安装的 Tesseract-OCR 目录（含 tesseract.exe）。"""
    candidates = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR"
        ),
    ]
    for base in candidates:
        if base:
            exe = Path(base) / "Tesseract-OCR" / "tesseract.exe"
            if exe.is_file():
                return exe.parent
    which = shutil.which("tesseract")
    if which:
        return Path(which).resolve().parent
    return None


def copy_engine(install_dir: Path) -> None:
    """复制 tesseract.exe + 运行所需 DLL 到 vendor/（不含 tessdata）。"""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    copied = [install_dir / "tesseract.exe"]
    copied += sorted(install_dir.glob("*.dll"))
    # 训练数据另走 tessdata_fast 下载，不复制安装目录的语料
    for src in copied:
        dst = VENDOR_DIR / src.name
        shutil.copy2(src, dst)
        print(f"  engine: {src.name} ({src.stat().st_size / 1024:.0f} KB)")


def strip_debug_sections(dll: Path) -> bool:
    """剥离 PE DLL 尾部的 DWARF 调试节区，等价 strip --strip-debug。

    UB-Mannheim 5.4.0 的 libtesseract-5.dll 内嵌 .debug_* 节区共约 100MB。
    这些节区位于文件尾部且运行时不参与加载，截断文件并修补 PE 头即可。
    结构不符合预期（调试节区不在尾部）时原样保留。
    带数字签名的 DLL 修补后签名必然失效，证书表一并移除（运行时无影响）。
    """
    try:
        import pefile
    except ImportError:
        print(f"  strip: 未安装 pefile，跳过 {dll.name} 瘦身")
        return False

    data = bytearray(dll.read_bytes())
    pe = pefile.PE(str(dll), fast_load=True)
    try:
        # COFF 字符串表：长节区名（/N 形式）指向这里
        str_off = (
            pe.FILE_HEADER.PointerToSymbolTable
            + pe.FILE_HEADER.NumberOfSymbols * 18
        )

        def sec_name(name8: bytes) -> str:
            n = name8.rstrip(b"\x00")
            if n.startswith(b"/"):
                o = int(n[1:])
                end = data.find(b"\x00", str_off + o)
                return bytes(data[str_off + o:end]).decode(errors="replace")
            return n.decode(errors="replace")

        # 一次性提取全部所需字段：pefile 用 mmap 读文件，
        # 必须 close 释放映射后才能写回，否则会 EINVAL
        secs = [
            (
                sec_name(s.Name),
                s.PointerToRawData,
                s.SizeOfRawData,
                s.VirtualAddress,
                s.Misc_VirtualSize,
                s.Characteristics,
            )
            for s in pe.sections
        ]
        sym_ptr = pe.FILE_HEADER.PointerToSymbolTable
        size_of_optional = pe.FILE_HEADER.SizeOfOptionalHeader
        align = pe.OPTIONAL_HEADER.SectionAlignment
        check_sum = pe.OPTIONAL_HEADER.CheckSum
        sec_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_SECURITY"]
        ]
        signed = bool(sec_dir.Size)
        sec_dir_off = sec_dir.get_file_offset()
    finally:
        pe.close()

    dbg_i = [i for i, s in enumerate(secs) if s[0].startswith(".debug")]
    if not dbg_i:
        return False
    cutoff = min(secs[i][1] for i in dbg_i if secs[i][2])
    tail = max(
        (s[1] + s[2] for i, s in enumerate(secs) if i not in dbg_i and s[2]),
        default=0,
    )
    if tail > cutoff:
        print(f"  strip: {dll.name} 调试节区不在文件尾部，跳过")
        return False

    # 按保留节区重算 SizeOfImage / SizeOfInitializedData
    size_of_image, init_data = 0, 0
    for i, s in enumerate(secs):
        if i in dbg_i:
            continue
        end = s[3] + s[4]
        size_of_image = max(size_of_image, (end + align - 1) // align * align)
        if s[5] & 0x40:  # IMAGE_SCN_CNT_INITIALIZED_DATA
            init_data += s[2]

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    coff, opt = e_lfanew + 4, e_lfanew + 4 + 20
    struct.pack_into(
        "<H", data, coff + 2, len(secs) - len(dbg_i)
    )  # NumberOfSections
    if sym_ptr >= cutoff:  # 符号表随截断丢失
        struct.pack_into("<I", data, coff + 8, 0)  # PointerToSymbolTable
        struct.pack_into("<I", data, coff + 12, 0)  # NumberOfSymbols
    struct.pack_into("<I", data, opt + 8, init_data)  # SizeOfInitializedData
    struct.pack_into("<I", data, opt + 56, size_of_image)  # SizeOfImage
    if check_sum:
        struct.pack_into("<I", data, opt + 64, 0)  # CheckSum
    # 修补 PE 头后 Authenticode 签名必然失效（哈希覆盖节表），
    # 证书表位于文件尾部、截断后丢失，安全目录项一并清零
    if signed:
        data[sec_dir_off : sec_dir_off + 8] = b"\x00" * 8
    # 清零被删除的节表项
    sec_tbl = opt + size_of_optional
    for i in dbg_i:
        data[sec_tbl + i * 40 : sec_tbl + (i + 1) * 40] = b"\x00" * 40

    orig_mb = len(data) / 1024 / 1024
    del data[cutoff:]
    dll.write_bytes(bytes(data))
    extra = "，移除失效数字签名" if signed else ""
    print(
        f"  strip: {dll.name} {orig_mb:.1f} MB -> {len(data) / 1024 / 1024:.1f} MB"
        f"（移除 {len(dbg_i)} 个调试节区{extra}）"
    )
    return True


def strip_bloated_dlls() -> None:
    """对 vendor/ 中体积异常的 DLL 尝试剥离调试节区。"""
    for dll in sorted(VENDOR_DIR.glob("*.dll")):
        if dll.stat().st_size > _STRIP_THRESHOLD:
            strip_debug_sections(dll)


def fetch_langs(langs: list[str]) -> None:
    tessdata = VENDOR_DIR / "tessdata"
    tessdata.mkdir(parents=True, exist_ok=True)
    for lang in langs:
        dst = tessdata / f"{lang}.traineddata"
        url = f"{TESSDATA_FAST_BASE}/{lang}.traineddata"
        if dst.is_file():
            print(f"  lang: {lang} 已存在，跳过")
            continue
        print(f"  lang: 下载 {lang} ...")
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            data = resp.read()
        dst.write_bytes(data)
        print(f"  lang: {lang} ({len(data) / 1024 / 1024:.2f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--langs",
        default=",".join(DEFAULT_LANGS),
        help=f"逗号分隔的语言列表，默认 {','.join(DEFAULT_LANGS)}",
    )
    args = parser.parse_args()
    langs = [x.strip() for x in args.langs.split(",") if x.strip()]

    install_dir = find_installed_tesseract()
    if install_dir is not None:
        print(f"engine source: {install_dir}")
        copy_engine(install_dir)
    elif (VENDOR_DIR / "tesseract.exe").is_file():
        print("未找到已安装的 Tesseract；vendor/ 已含引擎，跳过引擎复制")
    else:
        print("未找到已安装的 Tesseract，请先执行:")
        print("  winget install UB-Mannheim.TesseractOCR")
        print("安装完成后重新运行本脚本。")
        return 1
    strip_bloated_dlls()
    fetch_langs(langs)
    print(f"完成: {VENDOR_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
