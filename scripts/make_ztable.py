#!/usr/bin/env python3
"""床の遠近表 (= 地平線から d line 下の「奥行き」z の下位 8 bit) を
C のヘッダとして焼く。

なぜビルド時に焼くのか:
  z = ZK / d は割り算である。6809 に除算命令は無く、C で書くと CMOC の
  実行時ライブラリ (DIV16) を引き込む。表は 96 個で固定なのだから、
  **ビルド時に焼いてしまえば実行時の除算は 1 回も起きない**。

なぜ下位 8 bit だけなのか:
  床の帯 (明暗) の切り替えは z の bit7 を見て決める。bit7 は下位 8 bit の
  中にあり、上位は判定に一切効かない。よって表は 1 line あたり 1 byte で
  足り、96 byte で収まる。

使い方 (= Makefile 経由で自動実行):
  python3 scripts/make_ztable.py build/z_table.h
"""

import sys

ZK = 60000          # 奥行きの尺度 (= c_main.c の ZK と一致させること)
LINES = 96          # 描画枠の高さ (line)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_ztable.py <out.h>")
    out_path = sys.argv[1]

    vals = [0]
    for d in range(1, LINES):
        vals.append((ZK // d) & 0xFF)

    lines = []
    lines.append("/* ============================================================")
    lines.append(" * z_table.h — scripts/make_ztable.py が生成 (手で直さない)")
    lines.append(" *")
    lines.append(f" *   z_lo[d] = (ZK / d) & 0xFF   (ZK = {ZK}, d = 0..{LINES - 1})")
    lines.append(" *   d は地平線から下へ何 line 目か。d=0 は使わない。")
    lines.append(" *   床の帯の明暗は (z_lo[d] + zoff) の bit7 で決まる。")
    lines.append(" * ============================================================ */")
    lines.append("")
    lines.append("#ifndef Z_TABLE_H")
    lines.append("#define Z_TABLE_H")
    lines.append("")
    lines.append(f"#define Z_TABLE_ZK    {ZK}u")
    lines.append(f"#define Z_TABLE_LINES {LINES}")
    lines.append("")
    lines.append(f"static const unsigned char z_lo[{LINES}] = {{")
    for i in range(0, LINES, 12):
        chunk = ", ".join(f"0x{v:02X}" for v in vals[i:i + 12])
        lines.append(f"    {chunk},")
    lines.append("};")
    lines.append("")
    lines.append("#endif")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"make_ztable.py: {out_path} ({LINES} entries, ZK={ZK})")


if __name__ == "__main__":
    main()
