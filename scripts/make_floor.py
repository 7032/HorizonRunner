#!/usr/bin/env python3
"""床 (擬似 3D の市松模様) のための表を 2 つ焼く。

  build/floor_data.s   … サブ CPU が貼り付ける 6 byte の模様表 (lwasm 用)
  build/floor_table.h  … main 側が「どの模様を使うか」を決めるための表 (C 用)

両方を 1 つの script が吐くのは、**模様の番号 (index) が 2 つのファイルで
食い違うと、絵が静かに化ける**からである。番号の付け方をここ 1 箇所に
閉じ込め、asm と C は生成物を読むだけにする。

------------------------------------------------------------
なぜ 6 byte 単位なのか
------------------------------------------------------------
サブ側の塗りは PSHU (D/X/Y = 6 byte を 1 命令で書き出す) で行う。描画枠は
48 byte 幅 = 6 の倍数なので、6 byte の模様を 8 回押せば端数無しに 1 行が
埋まる。よって **模様の横周期は 6 byte を割り切る値 (1/2/3/6 byte) に
限る**。これが市松のマス目の横幅を 4/8/12/24 dot に量子化する理由である
(マス目 2 つで 1 周期なので、周期 1 byte = マス 4 dot)。

------------------------------------------------------------
遠近との対応
------------------------------------------------------------
地平線から d line 下のマス目の横幅は、透視投影では d に比例する。そこで
幅 = d / 2 を上の 6 段 (1,2,4,8,12,24 dot) に丸めて使う。1 dot と 2 dot の
段は市松として分解できず 50% の網に見えるが、それは遠景として正しい。

奥行き方向の縞は z = ZK / d の bit7 で決める。帯 1 本の厚みは画面上で
128 * d*d / ZK line であり、d が小さいと 1 line を切る。そこで
**厚みが 2 line を切る手前 (d < D_ALIAS) は奥行きの縞を諦め**、横方向の
網だけで描く (= 遠景は縞が溶けて一様に見えるのが物理的にも正しい)。
"""

import sys

ZK = 30000          # 奥行きの尺度 (小さいほど帯が厚く、黒い遠景が狭くなる)
LINES = 96          # 描画枠の高さ (line)
ROW_BYTES = 6       # PSHU 1 回で書ける byte 数 (= 模様の最大周期)

# マス目の横幅 (dot) と、その周期 (byte)。周期は 6 を割り切ること。
CLASS_W = [1, 2, 4, 8, 12, 24]
CLASS_P = [1, 1, 1, 2, 3, 6]


def pattern_bytes(width, phase_bytes):
    """マス幅 width dot の市松 1 行ぶん (6 byte) を、phase_bytes だけ
    右へずらして作る。"""
    out = []
    for k in range(ROW_BYTES):
        v = 0
        for b in range(8):
            dot = (k - phase_bytes) * 8 + b
            # 幅 width のマスが交互に並ぶ。負の位置も正しく折り返す。
            cell = (dot // width) % 2
            v = (v << 1) | (1 - cell)
        out.append(v)
    return out


def build_table():
    """(entries, base, stride) を返す。
    entries[i] = 6 byte の模様。番号は
        i = parity * stride + base[class] + phase
    で決まる。"""
    base, entries = [], []
    # 市松の模様 (class ごとの位相 x 反転) はサブ側の表へは**焼かない**。
    # 床は横一直線の縞だけで描くので、市松の 28 通り
    # (168 byte) はサブ側のコード枠を無駄に占めるだけだった。番号の起点
    # 表 (FLR_BASE) は互換のため残すが、模様表そのものは無地 3 通りのみ。
    # (市松を戻す時は pattern_bytes() を class ごとに呼んで entries へ足す)
    for c, (w, p) in enumerate(zip(CLASS_W, CLASS_P)):
        base.append(0)
    stride = 0
    # 無地の 3 つ。近景の床は縦の格子を入れた
    # 市松ではなく **横一直線の縞**とする (= この 2 つだけを使う)。
    #
    # 明るい方を $FF (べた白) ではなく **$AA (中間調の網)** にするのは、
    # 床を 1 行おきに間引いて塗っているためである。べた白で塗ると
    # 「間引かれた黒い行」と「べた白の行」の対比が強すぎて、横線が
    # ぎらついて見える。$AA なら網目が細かく混ざり、中間色として
    # 落ち着いて見える。
    #
    # さらに遠景用として $88 (= 4 dot に 1 dot) の細かい網を足す。近景の
    # 明るい帯と同じ $AA を遠景にも使うと床が一様に平らへ見えてしまい、
    # 奥行きが消える。遠いほど薄くすることで、地平線へ向かって暗く
    # 溶けていく階調が付く。
    solid_dark = len(entries)
    entries.append([0x00] * ROW_BYTES)
    entries.append([0xAA] * ROW_BYTES)
    entries.append([0x88] * ROW_BYTES)
    return entries, base, stride, solid_dark


def d_alias():
    """奥行きの帯の厚みが 2 line を切る境目 d を返す。"""
    for d in range(1, LINES):
        if 128 * d * d / ZK >= 2.0:
            return d
    return LINES


def cls_of(d):
    """地平線から d line 下のマス目の class を返す。"""
    w = max(1, d // 2)
    for c in range(len(CLASS_W) - 1, -1, -1):
        if w >= CLASS_W[c]:
            return c
    return 0


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_floor.py <out.s> <out.h>")
    s_path, h_path = sys.argv[1], sys.argv[2]

    entries, base, stride, solid_dark = build_table()
    da = d_alias()

    # ---- サブ側の模様表 (.s) ----
    L = []
    L.append("* ============================================================")
    L.append("* floor_data.s — scripts/make_floor.py が生成 (手で直さない)")
    L.append("*")
    L.append(f"*   床の模様 {ROW_BYTES} byte x {len(entries)} 通り (無地のみ。市松は焼かない)。")
    L.append("*   0 = 黒 / 1 = $AA の網 (明るい帯) / 2 = $88 の網 (遠景用)")
    L.append("*   サブ側は番号から 6 byte を引いて D/X/Y に載せ、PSHU で貼る。")
    L.append("* ============================================================")
    L.append("")
    L.append(f"FLR_STRIDE      equ     {stride}")
    L.append(f"FLR_ENTRIES     equ     {len(entries)}")
    L.append("")
    L.append("flr_pat:")
    for i, e in enumerate(entries):
        vals = ",".join(f"${v:02X}" for v in e)
        L.append(f"                fcb     {vals}      * {i}")
    with open(s_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    # ---- main 側の表 (.h) ----
    z = [0] + [(ZK // d) & 0xFF for d in range(1, LINES)]
    cls = [0] + [cls_of(d) for d in range(1, LINES)]

    H = []
    H.append("/* ============================================================")
    H.append(" * floor_table.h — scripts/make_floor.py が生成 (手で直さない)")
    H.append(" *")
    H.append(f" *   z_lo[d]   = (ZK / d) & 0xFF   (ZK = {ZK})")
    H.append(" *               床の奥行き。帯の明暗は (z_lo[d] + zoff) の bit7。")
    H.append(" *   cls_lo[d] = そこでのマス目の class (0-5)")
    H.append(" *   FLR_BASE[c] / FLR_PERIOD[c] = class ごとの模様番号の起点と周期")
    H.append(" *   FLR_D_ALIAS = 奥行きの帯が 2 line を切る境目 (これより手前は")
    H.append(" *                 奥行きの縞を諦め、横方向の網だけで描く)")
    H.append(" * ============================================================ */")
    H.append("")
    H.append("#ifndef FLOOR_TABLE_H")
    H.append("#define FLOOR_TABLE_H")
    H.append("")
    H.append(f"#define FLR_STRIDE    {stride}")
    H.append(f"#define FLR_SOLID_DARK  {solid_dark}      /* 暗い帯 (黒) */")
    H.append(f"#define FLR_SOLID_LIGHT {solid_dark + 1}      /* 明るい帯 ($AA の中間調) */")
    H.append(f"#define FLR_FAR_MESH    {solid_dark + 2}      /* 遠景の霞 ($88 の細かい網) */")
    H.append(f"#define FLR_D_ALIAS   {da}")
    H.append(f"#define FLR_LINES     {LINES}")
    H.append("")
    H.append(f"static const unsigned char FLR_BASE[{len(base)}] = {{"
             + ", ".join(str(b) for b in base) + "};")
    H.append(f"static const unsigned char FLR_PERIOD[{len(CLASS_P)}] = {{"
             + ", ".join(str(p) for p in CLASS_P) + "};")
    H.append("")
    for name, arr in (("z_lo", z), ("cls_lo", cls)):
        H.append(f"static const unsigned char {name}[{LINES}] = {{")
        for i in range(0, LINES, 12):
            H.append("    " + ", ".join(f"0x{v:02X}" for v in arr[i:i + 12]) + ",")
        H.append("};")
        H.append("")
    H.append("#endif")
    with open(h_path, "w", encoding="utf-8") as f:
        f.write("\n".join(H) + "\n")

    print(f"make_floor.py: {s_path} ({len(entries)} 通り x {ROW_BYTES} byte), "
          f"{h_path} (ZK={ZK}, D_ALIAS={da})")


if __name__ == "__main__":
    main()
