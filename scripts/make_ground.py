#!/usr/bin/env python3
"""地上物 (低木 / 木) が床と同じ速さで手前へ流れて来るための表を焼く。

なぜ表なのか:
  床の帯は「奥行き z を毎フレーム ZSPEED だけ減らし、画面上の位置を
  d = ZK / z で求める」動きをしている (scripts/make_floor.py)。地上物を
  床に貼り付いて見せるには、まったく同じ式で位置を進めればよいが、
  実行時に割り算をすると一撃でフレームレートが落ちる。

  そこで**「地平線から d line 下に何フレーム留まるか」を表にする**。
  d は 1 line ずつしか進まないので、実行時は「残りフレーム数を 1 減らし、
  0 になったら d を 1 進めて表から次の滞在フレーム数を取る」だけで済む。
  手前では 1 フレームに 2 line 以上進むこともあるが、その区間は滞在 0 と
  焼いてあり、実行時は 0 の間だけ d を進め続ける (2〜3 回で止まる)。

  横位置は消失点からの偏りが d に比例する (透視投影) ので、レーンごとに
  x[d] の表を持つ。2 次元配列は添字計算に掛け算が出るので、レーンごとの
  起点 (gnd_xbase[]) を別に持って 1 次元の表を引く。

出力 (build/ground_path.h):
  GND_ZSPEED          … この表が前提にしている 1 フレームの奥行きの進み
                         (c_main.c の ZSPEED と一致必須。#if で検査する)
  GND_D0 / GND_D1     … 湧く d と、消える d
  GND_LIFE            … 湧いてから消えるまでのフレーム数 (密度の見積り用)
  GND_LANES           … レーンの本数
  gnd_dwell[d]        … d に留まるフレーム数 (d < GND_D0 は使わない)
  gnd_x[base + d]     … 枠内 x (2 dot 単位)。絵の **中心** の位置
  gnd_xbase[lane]     … lane * 64 (掛け算を出さないための表)
"""

import sys

ZK = 30000          # 奥行きの尺度 (scripts/make_floor.py と同じ値)
ZSPEED = 12         # 1 フレームで進む奥行き (src/c_main.c の ZSPEED と同じ値)
D0 = 10             # 湧く位置 (地平線から下へ line)。ここより奥はほぼ動かない
D1 = 58             # 消える位置 (= 枠の下端付近)
TABLE = 64          # d を直接添字にするための表の長さ (d < 64)
X_CENTER = 96       # 消失点の x (2 dot 単位) = 枠中央 384/2/2
# レーン: 消失点からの横の偏り (d=1 あたり 2 dot 単位でいくつずれるか) の
# 1/4 倍値。左右 3 本ずつ。中央 (0) は自機の進路なので置かない。
LANES = [-6, -4, -2, 2, 4, 6]


def zof(d):
    return ZK / d


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_ground.py <out.h>")
    out_path = sys.argv[1]

    # 累積フレーム数 F(d) = (z(D0) - z(d)) / ZSPEED を丸め、差分を滞在数に
    # する。差分で持つことで丸め誤差が累積せず、総寿命が式どおりになる。
    F = [round((zof(D0) - zof(d)) / ZSPEED) for d in range(D0, D1 + 2)]
    dwell = [1] * TABLE
    for d in range(D0, D1 + 1):
        dwell[d] = F[d - D0 + 1] - F[d - D0]
    life = F[-1]
    if max(dwell) > 255:
        raise SystemExit("gnd_dwell が 8bit に収まらない")

    xs = []
    for lane in LANES:
        row = []
        for d in range(TABLE):
            v = X_CENTER + lane * d / 4.0
            row.append(max(0, min(191, int(round(v)))))
        xs += row

    H = []
    H.append("/* ============================================================")
    H.append(" * ground_path.h — scripts/make_ground.py が生成 (手で直さない)")
    H.append(" *")
    H.append(" *   地上物 (低木 / 木) が床と同じ速さで手前へ流れる表。")
    H.append(" *   gnd_dwell[d] は地平線から d line 下に留まるフレーム数。")
    H.append(" *   gnd_x[gnd_xbase[lane] + d] は絵の中心の x (2 dot 単位)。")
    H.append(" * ============================================================ */")
    H.append("")
    H.append("#ifndef GROUND_PATH_H")
    H.append("#define GROUND_PATH_H")
    H.append("")
    H.append(f"#define GND_ZSPEED  {ZSPEED}")
    H.append(f"#define GND_D0      {D0}")
    H.append(f"#define GND_D1      {D1}")
    H.append(f"#define GND_LIFE    {life}")
    H.append(f"#define GND_LANES   {len(LANES)}")
    H.append(f"#define GND_TABLE   {TABLE}")
    H.append("")

    def arr(name, values, per_line=16):
        H.append(f"static const unsigned char {name}[{len(values)}] = {{")
        for i in range(0, len(values), per_line):
            H.append("    " + ", ".join(str(v) for v in values[i:i + per_line]) + ",")
        H.append("};")
        H.append("")

    arr("gnd_dwell", dwell)
    arr("gnd_x", xs)
    H.append(f"static const unsigned int gnd_xbase[{len(LANES)}] = {{"
             + ", ".join(str(i * TABLE) for i in range(len(LANES))) + "};")
    H.append("")
    H.append("#endif")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(H) + "\n")
    print(f"make_ground.py: {out_path} (d={D0}..{D1}, life={life} frame, "
          f"lanes={len(LANES)}, {len(xs) + TABLE} byte)")


if __name__ == "__main__":
    main()
