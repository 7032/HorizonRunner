#!/usr/bin/env python3
"""サブ CPU 側メモリマップの検査 (= make が自動実行、失敗するとビルドが止まる)。

先行プロジェクトの検査から、本作で要る項目だけに削った版。

検査する内容は 2 つ:

1. **アドレス定数の突合せ**
   config.mk / src/asm_subprog.s の equ / src/c_subprog.h の #define に
   同じアドレスが書かれている。片方だけ直すと「転送先と参照先がずれる」
   という静かな壊れ方をする。エラーも例外も出ず、症状は「絵が化ける」
   だけである。よってビルドのたびに機械的に突き合わせる。
   **正は config.mk。**

2. **build/subprog.bin のサイズ検査**
   subprog コードは SUB_PROG_ADDR から上へ伸び、その先にはサブ CPU の
   ハードウェアスタック予約領域 (SUB_CODE_END = SUB_STACK_ADDR) がある。
   超えても lwasm も lwlink もエラーを出さない。スタックを踏むとサブ CPU
   ごと暴走するので、ここで機械的に止める。

先行プロジェクトが持っていたテープ多段ロードの突合せ検査は外してある
(= 土台では t77 は既定ビルド外。TAPE_* の値は先行プロジェクトから
 変えていないので、scripts/d77_to_t77_chunks.py / trampoline.asm の
 定数とはそのまま整合している)。テープを既定ビルドに戻す時は
 先行プロジェクトの検査 4 を持ち込むこと。

使い方 (= Makefile 経由で自動実行):
  python3 scripts/check_layout.py build/subprog.bin
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 3 箇所で一致していなければならないアドレス定数。
# (config.mk のキー, asm_subprog.s の equ 名, c_subprog.h の #define 名)
# None は「そのファイルには書かれていない」の意味。
KEYS = [
    ("SUB_WORK_ADDR",   "SUB_WORK_ADDR",   "SUB_WORK_ADDR"),
    ("SUB_TABLE_ADDR",  "SUB_TABLE_ADDR",  "SUB_TABLE_ADDR"),
    ("SUB_PROG_ADDR",   "SUB_PROG_ADDR",   "SUB_PROG_ADDR"),
    ("SUB_CODE_END",    "SUB_CODE_END",    "SUB_CODE_END"),
    ("SUB_STACK_ADDR",  None,              None),
    ("SUB_SPRITE_ADDR", None,              "SUB_SPRITE_ADDR"),
]


def die(msg: str) -> None:
    print(f"check_layout: NG — {msg}", file=sys.stderr)
    sys.exit(1)


def parse_config_mk() -> dict:
    vals = {}
    text = (ROOT / "config.mk").read_text(encoding="utf-8")
    for m in re.finditer(r"^\s*(SUB_\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                         text, re.M):
        vals[m.group(1)] = int(m.group(2), 0)
    return vals


def parse_asm_equ() -> dict:
    vals = {}
    text = (ROOT / "src" / "asm_subprog.s").read_text(encoding="utf-8")
    for m in re.finditer(r"^(SUB_\w+)\s+equ\s+\$([0-9A-Fa-f]+)", text, re.M):
        vals[m.group(1)] = int(m.group(2), 16)
    return vals


def parse_c_header() -> dict:
    vals = {}
    text = (ROOT / "src" / "c_subprog.h").read_text(encoding="utf-8")
    for m in re.finditer(r"^#define\s+(SUB_\w+)\s+(0x[0-9A-Fa-f]+|\d+)",
                         text, re.M):
        vals[m.group(1)] = int(m.group(2), 0)
    return vals


def main() -> None:
    if len(sys.argv) != 2:
        die("usage: check_layout.py <subprog.bin>")
    binpath = Path(sys.argv[1])

    mk = parse_config_mk()
    asm = parse_asm_equ()
    ch = parse_c_header()

    # 1. アドレス定数の突合せ (正は config.mk)
    for mk_key, asm_key, ch_key in KEYS:
        if mk_key not in mk:
            die(f"config.mk に {mk_key} が無い")
        want = mk[mk_key]
        if asm_key is not None:
            if asm_key not in asm:
                die(f"src/asm_subprog.s に equ {asm_key} が無い")
            if asm[asm_key] != want:
                die(f"{asm_key}: config.mk=${want:04X} だが "
                    f"asm_subprog.s=${asm[asm_key]:04X}")
        if ch_key is not None:
            if ch_key not in ch:
                die(f"src/c_subprog.h に #define {ch_key} が無い")
            if ch[ch_key] != want:
                die(f"{ch_key}: config.mk=${want:04X} だが "
                    f"c_subprog.h=${ch[ch_key]:04X}")

    # 前後関係 (領域が並び順どおりか)
    if not (mk["SUB_WORK_ADDR"] < mk["SUB_TABLE_ADDR"] < mk["SUB_PROG_ADDR"]
            < mk["SUB_CODE_END"] <= mk["SUB_STACK_ADDR"]):
        die("SUB_* の並び順が壊れている (WORK < TABLE < PROG < CODE_END <= STACK)")

    # 2. subprog.bin のサイズ検査
    if not binpath.exists():
        die(f"{binpath} が無い")
    size = binpath.stat().st_size
    top = mk["SUB_PROG_ADDR"] + size
    room = mk["SUB_CODE_END"] - mk["SUB_PROG_ADDR"]
    if top > mk["SUB_CODE_END"]:
        die(f"subprog.bin が {size} byte あり、"
            f"${mk['SUB_PROG_ADDR']:04X}+{size} = ${top:04X} が "
            f"SUB_CODE_END ${mk['SUB_CODE_END']:04X} を超える "
            f"(= サブ CPU のスタックを踏む)。上限 {room} byte。")

    print(f"check_layout: OK — subprog.bin {size} byte "
          f"(${mk['SUB_PROG_ADDR']:04X}-${top - 1:04X}, "
          f"残り {mk['SUB_CODE_END'] - top} byte)")


if __name__ == "__main__":
    main()
