#!/usr/bin/env python3
"""1 面のスクリプト (= 何を・いつ出すか) を、ビルド時に表として焼く。

スクリプトの形 (build/stage_script.h の stage_script[]):
    [符号, 次までのフレーム数] の並び。符号は次のとおり。
      0x00-0x3F : 敵・岩の軌道番号 (ENM_PATH_xxx。scripts/make_enemy.py)
      0x40-0x4F : 敵弾 (0 楕円 / 1 低空黒球 / 2 ボス火球)
      0x50-0x5F : 蛇型ボスの局面 (0 接近 / 1 ループ / 2 退避 / 3 連射 / 4 至近)
      0x80-0xBF : 地上物。bit3-5 = 種類 (0 低木 / 1 木)、bit0-2 = レーン (0-5)
      0xFE      : 何も出さずに待つだけ (255 フレームを超える間隔を刻む)
      0xFF      : 終わり (先頭へ戻る)

なぜ生成するのか:
    1 面は約 72 秒 = 4,300 フレームあり、地上物だけで 100 個近く出る。
    手で [符号, 間隔] を並べると必ず数え違える。ここでは **秒で書いた
    出来事の一覧** を並べ替えて間隔を計算し、表に落とす。地上物の
    「1 秒に 1〜2 個、左右ばらばら」は種を固定した擬似乱数で作るので、
    ビルドのたびに同じスクリプトになる。

時刻は面の開始からの秒。座標は scripts/make_enemy.py と同じ相対座標
(x 0..255) で書き、地上物のレーンはここで x から選ぶ。
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_enemy import PATHS  # noqa: E402
from make_ground import LANES as GND_LANES  # noqa: E402

FPS = 60
STAGE_LEN = 72.0            # 面の長さ (秒)
SC_WAIT = 0xFE
SC_END = 0xFF
SC_SHOT = 0x40              # 敵弾
SC_BOSS = 0x50              # ボス
SC_GND = 0x80
GND_BUSH, GND_TREE = 0, 1
LANES = len(GND_LANES)      # 地上物のレーン数 (scripts/make_ground.py)
PATH_ID = {name: i for i, (name, _, _, _) in enumerate(PATHS)}

events = []                 # (秒, 符号, 説明)


def at(t, code, note=""):
    events.append((t, code, note))


def enemy(t, name, n=1, gap=0.0):
    """軌道 name の敵を t 秒から n 体、gap 秒間隔で流す。"""
    for i in range(n):
        at(t + i * gap, PATH_ID[name], name)


def lane_of_x(x):
    """相対 x (0..255) を地上物のレーン (0-5) に丸める。中央は避ける。"""
    if x < 43: return 0
    if x < 85: return 1
    if x < 128: return 2
    if x < 171: return 3
    if x < 213: return 4
    return LANES - 1


def ground(t, kind, x):
    at(t, SC_GND | (kind << 3) | lane_of_x(x), "木" if kind else "低木")


def ground_span(t0, t1, kind, per_sec, rng):
    """t0..t1 の間、平均 per_sec 個/秒で地上物を左右ばらばらに置く。"""
    t = t0
    while t < t1:
        ground(t, kind, rng.choice([30, 60, 100, 150, 200, 235]))
        t += rng.uniform(0.7, 1.3) / per_sec


def build():
    rng = random.Random(7032)

    # ---- 開幕 (0-2 s): 低木 2-3、浮遊岩 群 1 ----
    ground(0.2, GND_BUSH, 50)
    ground(1.2, GND_BUSH, 220)
    ground(2.0, GND_BUSH, 40)
    enemy(0.5, "ROCK_1"); enemy(0.9, "ROCK_2"); enemy(1.3, "ROCK_3")

    # ---- 横断 A → 突進 (敵 A x5、横一列) ----
    enemy(2.25, "CROSS_A", 5, 0.1)

    # ---- 浮遊岩 群 2 ----
    enemy(7.5, "ROCK_4"); enemy(7.8, "ROCK_5"); enemy(8.1, "ROCK_2"); enemy(8.4, "ROCK_3")

    # ---- 敵 B (単体を 3 体、間を空けて)。急接近した至近で楕円弾 1 発ずつ ----
    enemy(8.0, "HOVER_B"); enemy(8.9, "HOVER_B"); enemy(9.8, "HOVER_B")
    for t in (10.7, 11.6, 12.5):
        at(t, SC_SHOT | 0, "敵弾 (楕円)")

    # ---- 浮遊岩 群 3 + 低木 ----
    for i, n in enumerate(["ROCK_6", "ROCK_7", "ROCK_1", "ROCK_8", "ROCK_2"]):
        enemy(12.5 + i * 0.3, n)
    ground_span(3.0, 14.5, GND_BUSH, 1.2, rng)

    # ---- 突進 A-右 (敵 A x8、0.4 s 間隔)。各機が楕円弾を撃つが、同時に
    #      飛ばせる敵弾は 6 発なので終盤の 3 機ぶんだけ出す ----
    enemy(14.75, "RUSH_AR", 8, 0.4)
    for t in (16.9, 17.25, 17.6):
        at(t, SC_SHOT | 0, "敵弾 (楕円)")

    # ---- 小休止 (低木のみ)、左に岩 2 個 ----
    ground_span(15.0, 24.0, GND_BUSH, 1.5, rng)
    enemy(21.25, "ROCK_6"); enemy(21.5, "ROCK_7")

    # ---- 滞空 C 群 1 (敵 C x4) ----
    for i, n in enumerate(["HOVER_C_TOP", "HOVER_C_LOW_L", "HOVER_C_LOW_R", "HOVER_C_RIGHT"]):
        enemy(24.0 + i * 0.2, n)
    at(26.3, SC_SHOT | 0, "敵弾 (楕円)")
    at(26.3, SC_SHOT | 0, "敵弾 (楕円)")
    at(26.5, SC_SHOT | 1, "敵弾 (黒球)")
    at(26.5, SC_SHOT | 1, "敵弾 (黒球)")
    enemy(24.7, "ROCK_4")

    # ---- 木の登場、浮遊岩 群 4 ----
    ground_span(24.5, 35.0, GND_BUSH, 1.0, rng)
    ground_span(28.5, 35.0, GND_TREE, 1.0, rng)
    for i, n in enumerate(["ROCK_2", "ROCK_3", "ROCK_4", "ROCK_8"]):
        enemy(30.0 + i * 0.25, n)

    # ---- 突進 A-左 (左 1 + 遠方 3) ----
    enemy(31.9, "RUSH_AL")
    enemy(32.4, "RUSH_AL_FAR", 3, 0.35)
    at(34.15, SC_SHOT | 0, "敵弾 (楕円)")

    # ---- 木・低木のみ (最密) ----
    ground_span(35.0, 38.5, GND_TREE, 2.0, rng)
    ground_span(35.0, 38.5, GND_BUSH, 1.0, rng)

    # ---- 横薙ぎ A (敵 A x6、0.25 s 間隔) ----
    enemy(38.75, "SWEEP_A", 6, 0.25)
    at(39.4, SC_SHOT | 0, "敵弾 (楕円)")

    # ---- 木・低木のみ ----
    ground_span(38.5, 46.5, GND_TREE, 1.0, rng)
    ground_span(38.5, 46.5, GND_BUSH, 0.8, rng)

    # ---- 滞空 C 群 2 (敵 C x4、弾多め) ----
    for i, n in enumerate(["HOVER_C_TOP", "HOVER_C_LOW_L", "HOVER_C_LOW_R", "HOVER_C_RIGHT"]):
        enemy(46.75 + i * 0.2, n)
    for t in (48.75, 49.5, 50.5, 51.5):
        at(t, SC_SHOT | 0, "敵弾 (楕円)")
    at(49.0, SC_SHOT | 1, "敵弾 (黒球)")
    at(50.0, SC_SHOT | 1, "敵弾 (黒球)")

    # ---- 木・低木のみ (最後の地上物は 54.5 s) ----
    ground_span(46.5, 54.5, GND_TREE, 1.0, rng)
    ground_span(46.5, 54.5, GND_BUSH, 0.8, rng)

    # ---- ボス戦 ----
    at(55.25, SC_BOSS | 0, "ボス 接近火球")
    at(59.5, SC_BOSS | 1, "ボス ループ")
    at(63.5, SC_BOSS | 2, "ボス 退避")
    at(65.5, SC_BOSS | 3, "ボス 5 連射")
    at(67.5, SC_BOSS | 4, "ボス 至近戦")
    at(STAGE_LEN, SC_WAIT, "面の終わり")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_stage.py <out.h>")
    out_path = sys.argv[1]
    build()
    events.sort(key=lambda e: e[0])

    # 秒 → フレームにし、[符号, 次までの間隔] に落とす。255 を超える間隔は
    # SC_WAIT で刻む。
    rows = []
    frames = [int(round(t * FPS)) for t, _, _ in events]
    prev = 0
    pending = None          # (符号, 説明) 間隔待ちの項目
    for (t, code, note), fr in zip(events, frames):
        gap = fr - prev
        if pending is None:
            # 先頭: 最初の出来事までの待ち
            while gap > 255:
                rows.append((SC_WAIT, 255, "待ち"))
                gap -= 255
            rows.append((SC_WAIT, gap, "開始待ち"))
        else:
            pc, pn = pending
            first = min(gap, 255)
            rows.append((pc, first, pn))
            gap -= first
            while gap > 0:
                w = min(gap, 255)
                rows.append((SC_WAIT, w, "待ち"))
                gap -= w
        pending = (code, note)
        prev = fr
    if pending is not None:
        rows.append((pending[0], 0, pending[1]))

    H = []
    H.append("/* ============================================================")
    H.append(" * stage_script.h — scripts/make_stage.py が生成 (手で直さない)")
    H.append(" *")
    H.append(" *   1 面のスクリプト。[符号, 次までのフレーム数] の並び。")
    H.append(" *   符号の意味は scripts/make_stage.py の先頭を参照。")
    H.append(" * ============================================================ */")
    H.append("")
    H.append("#ifndef STAGE_SCRIPT_H")
    H.append("#define STAGE_SCRIPT_H")
    H.append("")
    H.append(f"#define SC_WAIT     0x{SC_WAIT:02X}")
    H.append(f"#define SC_END      0x{SC_END:02X}")
    H.append(f"#define SC_SHOT     0x{SC_SHOT:02X}   /* 敵弾: 0x40-0x4F */")
    H.append(f"#define SC_BOSS     0x{SC_BOSS:02X}   /* ボス: 0x50-0x5F */")
    H.append(f"#define SC_GND      0x{SC_GND:02X}   /* 地上物: 0x80 | 種類<<3 | レーン */")
    H.append(f"#define STAGE_FRAMES {int(STAGE_LEN * FPS)}")
    H.append(f"#define STAGE_ROWS  {len(rows) + 1}")
    H.append("")
    H.append(f"static const unsigned char stage_script[{(len(rows) + 1) * 2}] = {{")
    for code, gap, note in rows:
        H.append(f"    0x{code:02X}, {gap:3d},   /* {note} */")
    H.append(f"    0x{SC_END:02X},   0,   /* 終わり → 先頭へ */")
    H.append("};")
    H.append("")
    H.append("#endif")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(H) + "\n")
    print(f"make_stage.py: {out_path} ({len(rows) + 1} rows, "
          f"{len(events)} events, {(len(rows) + 1) * 2} byte)")


if __name__ == "__main__":
    main()
