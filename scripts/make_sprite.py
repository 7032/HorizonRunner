#!/usr/bin/env python3
"""自機と敵の絵を、事前シフト済みビットマップとして焼く。

なぜ事前シフトか:
  横位置を 8 dot 単位 (= byte 境界) でしか動かせないと飛行がガタつく。
  1 dot 単位で動かすには「byte の途中から始まる絵」が要るが、実行時に
  ビットシフトすると 1 フレームぶんの予算を軽く食い潰す。そこで
  **ビルド時にずらした絵を全部焼いておき**、実行時は位相を選んで貼るだけ
  にする。

なぜマスクを持たないか:
  本作は **床と地平線を G プレーン (緑のカラープレーン) へ、自機・自弾・敵を
  B/R プレーン (白のダブルバッファリング面) へ**分けて描く。よってキャラクタを描く面は
  背景が常に真っ黒であり、OR で貼るだけで絵が出る。マスクは要らないので
  データは半分で済み、貼り付けも 1 byte あたり 4 命令から 3 命令に減る。

出力:
  spr_data … 自機。8 位相 x 20 line x 4 byte = 640 byte
  enm_data … 敵 (飛行体 / 球 / 敵 B) と地上物 (低木 / 木) と岩。各 3 段階、位相 4 通り
  enm_tab  … 敵の寸法表 (サブ CPU が引く)。1 件 6 byte:
               [w][h][位相ごとの byte 数 (2)][絵の先頭アドレス (2)]

  sprite_geo.h … main 側が引く寸法表 (obj_w[] / obj_h[])
  expl_data.h  … 爆発と敵弾の追加絵の中身と置場 (main が起動時に
                 $0A WRITE でサブ側 VRAM の末尾へ書き込む)。

追加絵の置場:
  サブ側のコード枠 ($C100-$CF7F) は絵で埋まり、空きがわずかしか無い。
  そこで爆発と敵弾の追加絵は **VRAM 各面の
  末尾 (表示されない 384 byte x 3 面、config.mk の SUB_TAIL*)** に置く。
  1 種類の絵 (位相 4 通りぶん) は 1 つの面の末尾に収まるよう割り付け、
  enm_tab にはその絶対番地を焼く。サブ側の貼り付けは番地が VRAM でも
  同じ手順で読める (実行中は VRAM ゲートが開いている)。

使い方 (= Makefile 経由で自動実行):
  python3 scripts/make_sprite.py build/sprite_data.s build/sprite_geo.h build/expl_data.h
"""

import sys

# ---- 自機 24 dot x 20 line ----
#   'X' = 描く / それ以外 = 透過。背後から見た飛行中の人物で、右手側に砲。
PLAYER = [
    ".........XXXXXX.........",
    "........XX....XX........",
    "........X.XX..X.........",
    ".........XXXXX..........",
    ".......XXXXXXXX.........",
    "....XXXXXXXXXXXXXX......",
    "...XX...XXXX...XXX......",
    "...XX...XXXX...XXXXXXX..",
    "...XXXXXXXXXXXXXXXXXXXX.",
    "....XXXXXXXXXXXXXXXXXXX.",
    ".....XXXXXXXXXX..XXXXX..",
    "......XXXXXXXX..........",
    "......XX...XXX..........",
    "......XX...XXX..........",
    "......XXXXXXXX..........",
    ".....XXXX..XXXX.........",
    "....XXXX....XXXX........",
    "...XXXX......XXXX.......",
    "...XXX........XXX.......",
    "..XXX..........XXX......",
]
PLAYER_PHASES = 8
PLAYER_BYTES = 4       # 24 dot + 最大 7 dot ずらし = 31 dot → 4 byte

# ---- 敵 (遠くから編隊で迫って来る飛行体) ----
#   奥行きに応じて 3 段階に描き分ける。小さいものは輪郭だけ、
#   大きいものほど中身が見えてくる。
ENEMY_ART = [
    [   # 0: 遠い (8 x 6)
        "..XXXX..",
        ".XXXXXX.",
        "XX.XX.XX",
        "XXXXXXXX",
        ".XX..XX.",
        "..X..X..",
    ],
    [   # 1: 中くらい (16 x 10)
        "....XXXXXXXX....",
        "..XXXXXXXXXXXX..",
        ".XXXX......XXXX.",
        "XXX..XXXXXX..XXX",
        "XX..XX....XX..XX",
        "XX..XX....XX..XX",
        "XXX..XXXXXX..XXX",
        ".XXXX......XXXX.",
        "..XX.XXXXXX.XX..",
        "...X..XXXX..X...",
    ],
    [   # 2: 近い (24 x 14)
        "......XXXXXXXXXXXX......",
        "....XXXXXXXXXXXXXXXX....",
        "..XXXXX..........XXXXX..",
        ".XXXX....XXXXXX....XXXX.",
        "XXXX...XXXXXXXXXX...XXXX",
        "XXX...XXX......XXX...XXX",
        "XXX..XXX........XXX..XXX",
        "XXX..XXX........XXX..XXX",
        "XXX...XXX......XXX...XXX",
        "XXXX...XXXXXXXXXX...XXXX",
        ".XXXX....XXXXXX....XXXX.",
        "..XXXXX..........XXXXX..",
        "....XXXX.XXXXXX.XXXX....",
        "......XX...XX...XX......",
    ],
]
# ---- 球 (3 つ 1 組で密集して迫る方) ----
#   同じく 3 段階。輪郭のはっきりした塊にして、飛行体と区別が付くようにする。
ORB_ART = [
    [   # 0: 遠い (8 x 6)
        "..XXXX..",
        ".XXXXXX.",
        "XXXXXXXX",
        "XXXXXXXX",
        ".XXXXXX.",
        "..XXXX..",
    ],
    [   # 1: 中くらい (16 x 10)
        ".....XXXXXX.....",
        "...XXXXXXXXXX...",
        "..XXXXXXXXXXXX..",
        ".XXXXX....XXXXX.",
        "XXXXX......XXXXX",
        "XXXXX......XXXXX",
        ".XXXXX....XXXXX.",
        "..XXXXXXXXXXXX..",
        "...XXXXXXXXXX...",
        ".....XXXXXX.....",
    ],
    [   # 2: 近い (24 x 14)
        "........XXXXXXXX........",
        ".....XXXXXXXXXXXXXX.....",
        "...XXXXXXXXXXXXXXXXXX...",
        "..XXXXXXX......XXXXXXX..",
        "..XXXXX..........XXXXX..",
        ".XXXXX............XXXXX.",
        ".XXXX..............XXXX.",
        ".XXXX..............XXXX.",
        ".XXXXX............XXXXX.",
        "..XXXXX..........XXXXX..",
        "..XXXXXXX......XXXXXXX..",
        "...XXXXXXXXXXXXXXXXXX...",
        ".....XXXXXXXXXXXXXX.....",
        "........XXXXXXXX........",
    ],
]

# ---- 地上物 (低木 / 木) と空中の岩 ----
#   敵と同じ 3 段階 (遠 / 中 / 近) で描き分け、同じ ENEMY コマンドで貼る。
#   FM-7 の画素は縦長 (1 : 約 2.4) なので、低木は横に広く・木は縦に高く
#   見えるよう line 数を抑えめに描く。すべて白 1 色なので、敵と見分けが
#   付くように中に暗い抜きを入れて質感を出す。
BUSH_ART = [
    [   # 6: 遠い (8 x 3)
        "..XXXX..",
        "XXXXXXXX",
        ".XXXXXX.",
    ],
    [   # 7: 中くらい (16 x 5)
        "....XX.XXXX.....",
        "..XXXXXXXXXXXX..",
        "XXXX.XXXXXX.XXXX",
        "XXXXXXXX.XXXXXXX",
        "..XXXXXXXXXXXX..",
    ],
    [   # 8: 近い (24 x 8)
        ".......XXX.XXXXX........",
        "....XXXXXXXXXXXXXXXX....",
        "..XXXXX.XXXXXXX.XXXXXX..",
        "XXXXXXXXXXXX.XXXXXXXXXXX",
        "XXX.XXXXXXXXXXXXXX.XXXXX",
        "XXXXXXXX.XXXXXXXXXXXXXXX",
        ".XXXXXXXXXXXX.XXXXXXXXX.",
        "...XXXXXXXXXXXXXXXXXX...",
    ],
]
TREE_ART = [
    [   # 9: 遠い (8 x 5)
        "..XXXX..",
        ".XXXXXX.",
        ".XXXXXX.",
        "...XX...",
        "...XX...",
    ],
    [   # 10: 中くらい (12 x 10)
        "....XXXX....",
        "..XXXXXXXX..",
        ".XXXX.XXXXX.",
        "XXXXXXXXXXXX",
        "XXXXXXX.XXXX",
        ".XXX.XXXXXX.",
        "..XXXXXXXX..",
        ".....XX.....",
        ".....XX.....",
        "....XXXX....",
    ],
    [   # 11: 近い (20 x 16)
        "........XXXX........",
        ".....XXXXXXXXXX.....",
        "...XXXXXXXXXXXXXX...",
        "..XXXXXXX..XXXXXXX..",
        ".XXXXXX......XXXXXX.",
        "XXXXXX........XXXXXX",
        "XXXXXXX......XXXXXXX",
        ".XXXXXXXX..XXXXXXXX.",
        "..XXXXXXXXXXXXXXXX..",
        "....XXXXXXXXXXXX....",
        "......XXXXXXXX......",
        ".........XX.........",
        ".........XX.........",
        ".........XX.........",
        "........XXXX........",
        ".......XXXXXX.......",
    ],
]
ROCK_ART = [
    [   # 12: 遠い (8 x 5)
        "...XXX..",
        ".XXXXXX.",
        "XXXXXXXX",
        "XXXXXXX.",
        ".XXXXX..",
    ],
    [   # 13: 中くらい (14 x 8)
        ".....XXXX.....",
        "...XXXXXXXX...",
        "..XXXX.XXXXXX.",
        ".XXXXXX.XXXXXX",
        "XXXXXXXX.XXXXX",
        "XXXXXXXXXXXXX.",
        ".XXXXXXXXXX...",
        "...XXXXXX.....",
    ],
    [   # 14: 近い (22 x 12)
        ".......XXXXXX.........",
        ".....XXXXXXXXXXX......",
        "...XXXXXXXXXXXXXXX....",
        "..XXXXXX..XXXXXXXXXX..",
        ".XXXXXXX...XXXXXXXXXX.",
        "XXXXXXXXX...XXXXXXXXXX",
        "XXXXXXXXXX...XXXXXXXXX",
        "XXXXXXXXXXX..XXXXXXXX.",
        ".XXXXXXXXXXXXXXXXXXX..",
        "..XXXXXXXXXXXXXXXXX...",
        "....XXXXXXXXXXXXX.....",
        ".......XXXXXXX........",
    ],
]
# ---- 自弾 ----
#   奥へ飛ぶにつれ 3 段階に縮む (16x3 / 8x2 / 8x1)。他の絵と同じ命令で
#   貼れるようここに置く (= キューの 1 命令にまとめるため)。
BULLET_ART = [
    [   # 15: 撃った直後 (16 x 3)
        "XXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXX",
    ],
    [   # 16: 途中 (8 x 2)
        "XXXXXXXX",
        "XXXXXXXX",
    ],
    [   # 17: 遠く (8 x 1)
        "XXXXXXXX",
    ],
]

# ---- 爆発 (自弾が当たった敵・岩・低木の跡、自機の被弾) ----
#   対象の段階 (遠 / 中 / 近) と同じ大きさで 2 こま (A: 芯が膨らむ /
#   B: 破片が散る) を持ち、A → B → A の順に数フレームずつ見せる。
#   木に当たった自弾は遠の A だけを 1〜2 フレーム見せて消える (= 弾が爆ぜる)。
#   置場は VRAM 各面の末尾 (上の説明)。
EXPL_A_ART = [
    [   # 18: 遠い A (8 x 6)
        "...XX...",
        "..XXXX..",
        ".XX..XX.",
        ".XX..XX.",
        "..XXXX..",
        "...XX...",
    ],
    [   # 19: 中くらい A (16 x 10)
        ".......XX.......",
        "....XXXXXXXX....",
        "...XXXX..XXXX...",
        "..XXX......XXX..",
        ".XXX........XXX.",
        ".XXX........XXX.",
        "..XXX......XXX..",
        "...XXXX..XXXX...",
        "....XXXXXXXX....",
        ".......XX.......",
    ],
    [   # 20: 近い A (24 x 14)
        "..........XXXX..........",
        ".......XXXXXXXXXX.......",
        ".....XXXXX....XXXXX.....",
        "....XXXX........XXXX....",
        "...XXX....XXXX....XXX...",
        "..XXX....XXXXXX....XXX..",
        "..XXX...XXX..XXX...XXX..",
        "..XXX...XXX..XXX...XXX..",
        "..XXX....XXXXXX....XXX..",
        "...XXX....XXXX....XXX...",
        "....XXXX........XXXX....",
        ".....XXXXX....XXXXX.....",
        ".......XXXXXXXXXX.......",
        "..........XXXX..........",
    ],
]
EXPL_B_ART = [
    [   # 21: 遠い B (8 x 6)
        "X..XX..X",
        ".X....X.",
        "...XX...",
        "...XX...",
        ".X....X.",
        "X..XX..X",
    ],
    [   # 22: 中くらい B (16 x 10)
        "XX....XXXX....XX",
        ".XX..........XX.",
        "...X..XXXX..X...",
        "......X..X......",
        "X....X....X....X",
        "X....X....X....X",
        "......X..X......",
        "...X..XXXX..X...",
        ".XX..........XX.",
        "XX....XXXX....XX",
    ],
    [   # 23: 近い B (24 x 14)
        "XX.......XXXXXX.......XX",
        ".XX....XX......XX....XX.",
        "..X...X..........X...X..",
        ".....X....XXXX....X.....",
        "X.......XX....XX.......X",
        "X.......X......X.......X",
        "........X..XX..X........",
        "........X..XX..X........",
        "X.......X......X.......X",
        "X.......XX....XX.......X",
        ".....X....XXXX....X.....",
        "..X...X..........X...X..",
        ".XX....XX......XX....XX.",
        "XX.......XXXXXX.......XX",
    ],
]

# ---- 敵の自機狙い楕円弾 ----
#   白い輪郭と黒い芯で、自機の直線的な白弾と見分ける。低空黒球弾は既存の
#   ORB_ART、ボス火球は EXPL_A/B_ART を交互に使うので、追加絵はこれだけ。
ESHOT_ART = [
    [   # 24: 遠い (8 x 3)
        "..XXXX..",
        "XX....XX",
        "..XXXX..",
    ],
    [   # 25: 中 (12 x 5)
        "...XXXXXX...",
        ".XXX....XXX.",
        "XX........XX",
        ".XXX....XXX.",
        "...XXXXXX...",
    ],
    [   # 26: 手前 (16 x 7)
        "....XXXXXXXX....",
        "..XXX......XXX..",
        ".XX..........XX.",
        "XX............XX",
        ".XX..........XX.",
        "..XXX......XXX..",
        "....XXXXXXXX....",
    ],
]

# ---- 敵 B (単体で遠方に滞空してから急接近する小型の飛行体) ----
#   敵 A より小ぶりで、翼を斜め下へ張った形にして見分ける。3 段階のうち
#   遠・中はサブ側のコード枠 (床の市松模様表を撤去して空けた分) に、
#   近は VRAM 末尾に置く。近を 16 dot 幅に抑えるのは、位相ずらしを含めて
#   3 byte に収め VRAM 末尾の空き (144 byte) に入れるためである。
ENEMY_B_ART = [
    [   # 27: 遠い (8 x 5)
        "...XX...",
        "X..XX..X",
        "XXXXXXXX",
        ".XX..XX.",
        ".X....X.",
    ],
    [   # 28: 中くらい (12 x 8)
        ".....XX.....",
        "....XXXX....",
        "XX..XXXX..XX",
        "XXXXX..XXXXX",
        ".XXXXXXXXXX.",
        "...XX..XX...",
        "..XX....XX..",
        "..X......X..",
    ],
    [   # 29: 近い (16 x 11)
        ".......XX.......",
        "......XXXX......",
        ".....XXXXXX.....",
        "XX...XX..XX...XX",
        "XXX..XX..XX..XXX",
        "XXXXXXX..XXXXXXX",
        ".XXXXXXXXXXXXXX.",
        "...XXX....XXX...",
        "...XX......XX...",
        "..XX........XX..",
        "..X..........X..",
    ],
]

ENEMY_PHASES = 4        # 2 dot 刻み (敵は小さく速いのでこれで足りる)
ENEMY_PHASE_STEP = 2


def art_bits(art):
    w = len(art[0])
    for row in art:
        if len(row) != w:
            raise SystemExit(f"絵の幅が揃っていない: {row!r}")
    return [[1 if c == "X" else 0 for c in row] for row in art], w


def shift_to_bytes(bits, phase, dst_bytes):
    """bit 列を右へ phase dot ずらし、dst_bytes byte に詰める。"""
    width = dst_bytes * 8
    line = [0] * width
    for i, b in enumerate(bits):
        line[i + phase] = b
    return [int("".join(str(v) for v in line[k * 8:(k + 1) * 8]), 2)
            for k in range(dst_bytes)]


def emit_sprite(L, label, art, phases, phase_step, dst_bytes):
    bits, w = art_bits(art)
    L.append(f"{label}:")
    for p in range(phases):
        L.append(f"* ---- 位相 {p * phase_step} dot ----")
        for row in bits:
            vals = ",".join(f"${v:02X}"
                            for v in shift_to_bytes(row, p * phase_step, dst_bytes))
            L.append(f"                fcb     {vals}")
    return len(art) * dst_bytes


def read_tails():
    """config.mk から VRAM 末尾の置場 (SUB_TAIL*_ADDR / SUB_TAIL_SIZE) を読む。
    ここに直書きしない (= 置場の正は config.mk 1 か所)。"""
    import os
    import re
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "config.mk")
    vals = {}
    with open(cfg, encoding="utf-8") as f:
        for m in re.finditer(r"^\s*(SUB_TAIL\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)",
                             f.read(), re.M):
            vals[m.group(1)] = int(m.group(2), 0)
    tails = [vals[k] for k in ("SUB_TAIL0_ADDR", "SUB_TAIL1_ADDR",
                               "SUB_TAIL2_ADDR")]
    return tails, vals["SUB_TAIL_SIZE"]


def main():
    if len(sys.argv) != 4:
        raise SystemExit("usage: make_sprite.py <out.s> <out.h> <expl.h>")
    out_path = sys.argv[1]
    hdr_path = sys.argv[2]
    expl_path = sys.argv[3]
    tails, tail_size = read_tails()

    L = []
    L.append("* ============================================================")
    L.append("* sprite_data.s — scripts/make_sprite.py が生成 (手で直さない)")
    L.append("*")
    L.append("*   自機と敵の絵。事前シフト済みで、実行時のビットシフトは無い。")
    L.append("*   キャラクタは B/R プレーン (白) にのみ描き、背景は常に黒なので")
    L.append("*   **マスクは持たない** (OR で貼るだけ)。")
    L.append("* ============================================================")
    L.append("")
    L.append(f"SPR_W_BYTES     equ     {PLAYER_BYTES}")
    L.append(f"SPR_H_LINES     equ     {len(PLAYER)}")
    L.append(f"SPR_PHASE_BYTES equ     {len(PLAYER) * PLAYER_BYTES}")
    L.append(f"SPR_PHASES      equ     {PLAYER_PHASES}")
    L.append(f"ENM_PHASES      equ     {ENEMY_PHASES}")
    L.append(f"ENM_KINDS       equ     {len(ENEMY_ART) + len(ORB_ART)}")
    L.append("")

    emit_sprite(L, "spr_data", PLAYER, PLAYER_PHASES, 1, PLAYER_BYTES)
    L.append("")

    # 敵: 寸法表 → 絵の順に置く。飛行体 3 段階 + 球 3 段階 = 6 種。
    kinds = ENEMY_ART + ORB_ART + BUSH_ART + TREE_ART + ROCK_ART + BULLET_ART
    n_local = len(kinds)                # ここまでがサブ側コード枠に置く絵
    kinds = kinds + EXPL_A_ART + EXPL_B_ART + ESHOT_ART
    n_tail_end = len(kinds)             # 18-26 は VRAM 末尾
    kinds = kinds + ENEMY_B_ART         # 27-29 = 敵 B (遠・中は枠 / 近は末尾)
    # VRAM 末尾へ置く絵の番号の集合。それ以外はサブ側のコード枠に置く。
    tail_kinds = set(range(n_local, n_tail_end)) | {n_tail_end + 2}
    local_kinds = [k for k in range(len(kinds)) if k not in tail_kinds]
    geo = []
    for k, art in enumerate(kinds):
        w = len(art[0])
        dst = (w + (ENEMY_PHASES - 1) * ENEMY_PHASE_STEP + 7) // 8
        geo.append((w, len(art), dst, len(art) * dst))

    # 追加絵を VRAM 末尾へ割り付ける (1 種類 = 位相 4 通りが 1 つの面に
    # 収まるよう、大きい順に詰める)。番地は enm_tab へ絶対値で焼く。
    tail_use = [0] * len(tails)
    expl_addr = {}                      # k → 絶対番地
    for k in sorted(tail_kinds, key=lambda k: -geo[k][3] * ENEMY_PHASES):
        need = geo[k][3] * ENEMY_PHASES
        for t in range(len(tails)):
            if tail_use[t] + need <= tail_size:
                expl_addr[k] = tails[t] + tail_use[t]
                tail_use[t] += need
                break
        else:
            raise SystemExit(f"追加絵 {k} ({need} byte) が VRAM 末尾に入らない")

    L.append("* 敵の寸法表 (1 件 6 byte): [w][h][位相ごとの byte 数][絵の先頭]")
    L.append("*   0-2 = 飛行体 / 3-5 = 球 / 6-8 = 低木 / 9-11 = 木 / 12-14 = 岩")
    L.append("*   15-17 = 自弾 (大 / 中 / 小)")
    L.append("*   18-20 = 爆発 A / 21-23 = 爆発 B / 24-26 = 敵の楕円弾")
    L.append("*   27-29 = 敵 B")
    L.append("*   (18-26 と 29 の絵の中身は VRAM 末尾。番地は絶対値)")
    L.append("*   (それぞれ 遠 / 中 / 近 の順)")
    L.append("enm_tab:")
    for k, (w, h, dst, stride) in enumerate(geo):
        # サブ側は位相ずらしを 8bit の mul で行うので、位相ごとの byte 数は
        # 256 未満でなければならない (fdb の上位は 0 のまま置く)。
        if stride >= 256:
            raise SystemExit(f"絵 {k} の位相ごとの byte 数 {stride} が 255 を超える")
        L.append(f"                fcb     {dst},{h}")
        L.append(f"                fdb     {stride}")
        if k not in tail_kinds:
            L.append(f"                fdb     enm_k{k}")
        else:
            L.append(f"                fdb     ${expl_addr[k]:04X}")
    L.append("")
    total_enm = 0
    for k in local_kinds:
        art = kinds[k]
        total_enm += emit_sprite(L, f"enm_k{k}", art, ENEMY_PHASES,
                                 ENEMY_PHASE_STEP, geo[k][2]) * ENEMY_PHASES
        L.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    # ---- main 側が引く寸法表 (.h) ----
    #   枠からはみ出す判定と、右端で寄せる幅の計算に使う。ここから焼くので
    #   絵を直しても C 側と食い違わない。
    H = []
    H.append("/* ============================================================")
    H.append(" * sprite_geo.h — scripts/make_sprite.py が生成 (手で直さない)")
    H.append(" *")
    H.append(" *   ENEMY コマンドで貼る絵の寸法。k = 種類番号。")
    H.append(" *   0-2 飛行体 / 3-5 球 / 6-8 低木 / 9-11 木 / 12-14 岩 (遠/中/近)")
    H.append(" *   15-17 自弾 / 18-20 爆発 A / 21-23 爆発 B / 24-26 敵の楕円弾")
    H.append(" *   27-29 敵 B")
    H.append(" * ============================================================ */")
    H.append("")
    H.append("#ifndef SPRITE_GEO_H")
    H.append("#define SPRITE_GEO_H")
    H.append("")
    H.append(f"#define OBJ_KINDS       {len(kinds)}")
    H.append("#define OBJ_K_FLYER     0")
    H.append("#define OBJ_K_ORB       3")
    H.append("#define OBJ_K_BUSH      6")
    H.append("#define OBJ_K_TREE      9")
    H.append("#define OBJ_K_ROCK      12")
    H.append("#define OBJ_K_BULLET    15")
    H.append("#define OBJ_K_EXPL_A    18")
    H.append("#define OBJ_K_EXPL_B    21")
    H.append("#define OBJ_K_ESHOT     24")
    H.append("#define OBJ_K_FLYER_B   27")
    H.append("")
    H.append("/* 貼り付け幅 (byte)。位相ずらしを含む */")
    H.append(f"static const unsigned char obj_w[{len(kinds)}] = {{ "
             + ", ".join(str(g[2]) for g in geo) + " };")
    H.append("/* 絵の幅の半分 (2 dot 単位)。中心の x から左端を求めるのに使う */")
    H.append(f"static const unsigned char obj_hw[{len(kinds)}] = {{ "
             + ", ".join(str(int(round(g[0] / 4.0))) for g in geo) + " };")
    H.append("/* 高さ (line) */")
    H.append(f"static const unsigned char obj_h[{len(kinds)}] = {{ "
             + ", ".join(str(g[1]) for g in geo) + " };")
    H.append("")
    H.append("#endif")
    with open(hdr_path, "w", encoding="utf-8") as f:
        f.write("\n".join(H) + "\n")

    # ---- 追加絵 (= main が起動時に WRITE でサブ側 VRAM 末尾へ送る) ----
    #   面ごとに [番地, byte 数] と中身の並びを焼く。中身は enm_tab の
    #   番地割り付けと同じ順 (= 面ごとに番地の昇順) で連結する。
    E = []
    E.append("/* ============================================================")
    E.append(" * expl_data.h — scripts/make_sprite.py が生成 (手で直さない)")
    E.append(" *")
    E.append(" *   爆発 (18-23) と敵の楕円弾 (24-26) と敵 B の近 (29)、各 4 位相。置場は VRAM 各面の")
    E.append(" *   末尾 (表示されない領域) で、main が起動時に $0A WRITE 命令で")
    E.append(" *   expl_seg[] の [番地, byte 数] ごとに送り込む。")
    E.append(" * ============================================================ */")
    E.append("")
    E.append("#ifndef EXPL_DATA_H")
    E.append("#define EXPL_DATA_H")
    E.append("")
    segs = []
    blob = []
    for t, base in enumerate(tails):
        ks = sorted((k for k in expl_addr if base <= expl_addr[k] < base + tail_size),
                    key=lambda k: expl_addr[k])
        if not ks:
            continue
        start = len(blob)
        for k in ks:
            assert expl_addr[k] == base + (len(blob) - start)
            bits, w = art_bits(kinds[k])
            for p in range(ENEMY_PHASES):
                for row in bits:
                    blob += shift_to_bytes(row, p * ENEMY_PHASE_STEP, geo[k][2])
        segs.append((base, len(blob) - start, start))
    E.append(f"#define EXPL_SEGS   {len(segs)}")
    E.append(f"#define EXPL_BYTES  {len(blob)}")
    E.append("/* [サブ側の番地 (上位, 下位), byte 数 (上位, 下位)] x EXPL_SEGS。"
             "expl_data[] を先頭から順に切って送る */")
    E.append(f"static const unsigned char expl_seg[{len(segs) * 4}] = {{")
    for base, n, off in segs:
        E.append(f"    0x{base >> 8:02X}, 0x{base & 0xFF:02X}, "
                 f"0x{n >> 8:02X}, 0x{n & 0xFF:02X},")
    E.append("};")
    E.append(f"static const unsigned char expl_data[{len(blob)}] = {{")
    for i in range(0, len(blob), 16):
        E.append("    " + ", ".join(f"0x{v:02X}" for v in blob[i:i + 16]) + ",")
    E.append("};")
    E.append("")
    E.append("#endif")
    with open(expl_path, "w", encoding="utf-8") as f:
        f.write("\n".join(E) + "\n")

    hb = PLAYER_PHASES * len(PLAYER) * PLAYER_BYTES
    print(f"make_sprite.py: {out_path} "
          f"(自機 {hb} byte / 敵 {total_enm} byte / 爆発 {len(blob)} byte "
          f"→ VRAM 末尾 {[f'${a:04X}' for a in tails]} 使用 {tail_use})")


if __name__ == "__main__":
    main()
