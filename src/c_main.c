/* ============================================================
 * c_main.c — HorizonRunner 本体 (地平線 + 床スクロール + 自機 + 自弾 + 敵編隊
 *            + 地上物 (低木 / 木) + 空中の岩 + 当たり判定と爆発)
 *
 * 画面の作り (= 描画枠 384x96 dot、枠左上は画面中央の 52 line / 16 byte):
 *
 *      +----------------------------------+  枠の上端 (= 空。何も描かない)
 *      |                                  |
 *      |          (自機・自弾・敵)        |
 *      |==================================|  地平線 (緑)
 *      |                                  |  遠景 (帯が分解できないので黒)
 *      |----------------------------------|  床の帯 (緑。奥行きスクロール)
 *      |==================================|
 *      +----------------------------------+  枠の下端
 *
 * ---- 三枚のプレーンの役割 ----
 *   B / R plane … ダブルバッファリングの表と裏。**自機・自弾・敵を白で描く**
 *   G plane     … **地平線と床のカラープレーン (緑)**。ダブルバッファリングしない
 *
 * 白の面は毎フレーム裏で描き切ってからパレット差し替えで表に出すので、
 * キャラクタは一切ちらつかない。床はカラープレーンに毎フレーム塗り直す。
 * 面に描いたキャラクタの消し込みは **サブ側が自分で憶えている**ので、
 * main から消し込みコマンドを送る必要は無い (docs/RENDER.md)。
 *
 * ---- 操作 ----
 * 初代 FM-7 は離鍵コードを返さないので方向はラッチ保持である。
 *   8/2/4/6 = 軸ごとに置換 / 7/9/1/3 = 両軸まとめて置換 (斜め) / 5 = 停止
 *   BREAK   = 自弾の発射 (押しっぱなしでオート連射)
 *
 * 自機の上下に連動して地平線が逆へ動く (= カメラのピッチ)。前進は常時
 * 掛かっており、床の帯が手前へ流れ続ける。
 * ============================================================ */

#include "c_subsys.h"
#include "c_subprog.h"
#include "c_input.h"
#include "c_sound.h"
#include "c_text.h"
/* 床の遠近表・模様番号 (= scripts/make_floor.py がビルド時に焼く) と、
 * 敵の軌道表 (= scripts/make_enemy.py)。実行時に乗除算をしないための表で
 * あり、手で書き換えない。 */
#include "../build/floor_table.h"
#include "../build/enemy_path.h"
/* 地上物の滞在フレーム表 (= scripts/make_ground.py) と、ENEMY コマンドで
 * 貼る絵の寸法表 (= scripts/make_sprite.py)。どちらも生成物。 */
#include "../build/ground_path.h"
#include "../build/sprite_geo.h"
/* 1 面のスクリプト (= scripts/make_stage.py)。秒で書いた出来事の一覧を
 * [符号, 次までのフレーム数] の表に落としたもの。 */
#include "../build/stage_script.h"
/* 爆発と敵弾の追加絵の中身と置場 (= scripts/make_sprite.py)。サブ側の
 * コード枠に入り切らないので、起動時に WRITE で VRAM 各面の末尾へ送る。 */
#include "../build/expl_data.h"

/* フレームペーシング: 1 tick = 約 2ms、8 tick = 約 16.3ms = 約 61fps */
#define FRAME_TARGET        8
#define PACE_SAFETY_CAP     8000u

/* ---- 自機の可動範囲と速度 ----
 * 横は dot 単位 (= 8 位相の事前シフトがあるので 1 dot 刻みで置ける)。
 * 縦は line 単位。FM-7 の画素は縦長 (1 : 約 2.4) なので、横を縦より大きく
 * 刻まないと見かけの速度が釣り合わない。 */
#define HX_MIN      0
#define HX_MAX      (FRAME_W_DOTS - SPR_W_DOTS - 8)   /* 352 */
#define HY_MIN      0
#define HY_MAX      (FRAME_H_LINES - SPR_H_LINES - 1) /* 75 */
#define HSTEP       5                                  /* 横 5 dot / frame */
#define VSTEP       2                                  /* 縦 2 line / frame */

/* ---- 地平線 (= カメラのピッチ) ----
 * 自機が上へ行くほど地平線は下がる。必ず偶数行へ丸める (床の間引きの
 * 位相を揃えるため)。 */
#define HY_MID      ((HY_MIN + HY_MAX) / 2)   /* 37 */
#define HZ_MID      30
#define HZ_MIN      20
#define HZ_MAX      44

/* ---- 床 (擬似 3D、横一直線の縞) ----
 * 地平線から d line 下の床の「奥行き」は z = ZK / d である (= 透視投影)。
 * 帯の明暗は (z_lo[d] + zoff) の bit7 で決まり、zoff を毎フレーム進めると
 * 帯が手前へ流れる。帯 1 本の厚みは画面上で 128 * d*d / ZK line なので、
 * 遠いほど薄く手前ほど厚い = 遠近が付く。
 *
 * 地平線に近い側 (d < FLR_D_ALIAS) は帯の厚みが 1 line を切って明滅に
 * なるため、**何も描かず黒のまま残す**。空と、地平線の下の暗がりだけが
 * 見える見通しの良い画になる。
 *
 * 床は 1 行おきに間引いて塗る。塗る量が半分になるので床の塗りは 2 倍速く
 * なる。以後「行」と書いたら、この間引き後の論理行である。 */
#define FLR_FAR_PAT FLR_SOLID_DARK  /* 遠景は黒 (= 何も描かない) */

/* 1 フレームで進む奥行き (= z 単位)。地上物の滞在表もこの値で焼いてある。 */
#define ZSPEED      12
#if ZSPEED != GND_ZSPEED
#error "ZSPEED と scripts/make_ground.py の ZSPEED が食い違っている"
#endif

/* 帯の数の上限 (= キューは 108 byte。帯 1 本 2 byte + 他コマンド分) */
#define MAX_BANDS   24

/* ---- 自弾 ----
 * この手のゲームの自弾は「奥へ飛んでいく」。撃った位置から **画面中央**
 * へ向かって直線的に縮んでいけばそう見える。収束先を地平線にすると上に
 * 寄り過ぎるので、枠のちょうど真ん中を狙う。
 *
 * 座標も進み幅も 8bit で持つ。16bit にすると加算も比較も 2 倍以上高く付き、
 * 4 発ぶんで 60fps が 51fps まで落ちた。横位置は 2 dot 単位 (= half dot)
 * で持てば 0-191 に収まり、自弾は byte 境界に貼るので精度は十分である。 */
#define MAX_BUL     4
#define BUL_LIFE    16                      /* 収束点へ届くまでのフレーム数 */
#define VANISH_HX   (FRAME_W_DOTS / 4)      /* 画面中央 x (2 dot 単位) = 96 */
#define VANISH_Y    (FRAME_H_LINES / 2)     /* 画面中央 y (line) = 48 */

static unsigned char bul_hx[MAX_BUL];   /* 枠内 x (2 dot 単位、0-191) */
static unsigned char bul_y[MAX_BUL];    /* 枠内 line (0-95) */
static unsigned char bul_dx[MAX_BUL];   /* 1 フレームの進み幅 (2 の補数) */
static unsigned char bul_dy[MAX_BUL];
static unsigned char bul_life[MAX_BUL]; /* 0 = 空きスロット */
/* 当たり判定用 (毎フレーム、進めた直後に 1 回だけ求めて憶える) */
static unsigned char bul_k[MAX_BUL];    /* 今フレームの絵の種類 (15-17) */
static unsigned char bul_x1[MAX_BUL];   /* 右端 (2 dot 単位、= hx + 幅) */
static unsigned char bul_y1[MAX_BUL];   /* 下端 (= y + 高さ) */
static unsigned char bul_st[MAX_BUL];   /* 奥行きの段階 (2 手前 / 1 中 / 0 奥) */
static unsigned char bul_pop[MAX_BUL];  /* 木に当たって爆ぜている残りフレーム (0 = 通常) */

/* ---- 当たり判定 (= 8bit の矩形比較だけ。乗除算は無い) ----
 * 奥行きは「段階」で比べる。敵・岩は軌道表の段階 (0 奥 / 1 中 / 2 手前)、
 * 地上物は d を GND_STAGE_* で刻んだ段階、自弾は残り寿命で刻んだ段階
 * (撃った直後 = 2、収束点の近く = 0)。**自弾の段階が対象と同じか 1 つ
 * 手前** なら奥行きが合っているとみなし、あとは画面上の矩形 (x は
 * 2 dot 単位、y は line) が重なれば命中。矩形の幅は sprite_geo.h の
 * obj_w (byte) x 4、高さは obj_h をそのまま使う。
 *
 * 自機は「対象が最も手前の段階 (2)」の時だけ当たる。自機の矩形は絵より
 * ひと回り小さく取る (= 理不尽な被弾を減らす)。 */
#define BUL_ST(life)   ((life) >= 12 ? 2 : ((life) >= 7 ? 1 : 0))
#define PL_INSET_X     3            /* 自機の当たり枠の内寄せ (2 dot 単位) */
#define PL_INSET_Y     4            /* 同 (line) */
#define PL_INV_FRAMES  180          /* 復帰後の無敵 (点滅) フレーム数 = 約 3 秒 */
#define PL_DIE_FRAMES  45           /* 被弾の演出。この間は敵ごと画面を止める */

/* ---- 爆発 ----
 * 撃たれた敵・岩・低木は、そのスロットのまま「爆発している状態」になり、
 * 同じ位置に爆発の絵を EXP_LEN フレーム見せて消える (= 表を増やさない)。
 * 絵は対象の段階と同じ大きさで、A (芯) → B (破片) → A の 3 段階。 */
#define EXP_LEN        10
#define EXP_KIND(t)    (((t) > 7 || (t) < 4) ? OBJ_K_EXPL_A : OBJ_K_EXPL_B)
#define POP_LEN        2            /* 木に当たった弾が爆ぜて見える長さ */

/* ---- 敵と岩 ----
 * 奥から迫って来る。軌道は build/enemy_path.h の表を引くだけで、実行時の
 * 乗除算は 1 回も無い。y は地平線からの相対 (2 の補数) なので、カメラの
 * ピッチにそのまま追従して地面に貼り付いて見える。空を飛ぶ物は y が
 * 負なだけで、同じ仕組みで動く。軌道ごとに歩数と歩の間隔 (毎フレーム /
 * 2 フレームに 1 歩) と絵の種類が表に焼いてある。
 *
 * 自弾と自機の当たり判定も、軌道表の段階と 8bit の矩形だけで行う。 */
#define MAX_ENM     8

static unsigned char enm_step[MAX_ENM];  /* 0 = 空き。1..plen が生存 */
static unsigned char enm_path[MAX_ENM];
static unsigned char enm_ex[MAX_ENM];    /* 爆発の残りフレーム (0 = 生きている) */

/* ---- 地上物 (低木 / 木) ----
 * 床と同じ速さで手前へ流れる景色であり、床に貼り付いて見えることが
 * 何より大事である。そこで床と同じ式 (z を毎フレーム ZSPEED 減らし、
 * d = ZK / z) を **「d に留まるフレーム数」の表** (gnd_dwell[]) に焼き、
 * 実行時は残りフレームを 1 減らして 0 なら d を 1 進めるだけにする。
 * 横位置はレーンごとの表 x[d] を引く (透視投影で d に比例するため)。
 * 絵の底が地面 (地平線から d line 下) に着くよう、上端は d - 高さ に置く。
 *
 * 種類ごとの性質は gnd_hard[] に持つ (低木は撃つと壊れる / 木は壊れない)。
 * 当たり判定ではここを見る。 */
#define MAX_GND     6
#define GND_KINDS   2

static unsigned char gnd_d[MAX_GND];      /* 0 = 空き。GND_D0..GND_D1 が生存 */
static unsigned char gnd_cnt[MAX_GND];    /* いまの d に留まる残りフレーム数 */
static unsigned char gnd_kind[MAX_GND];   /* 0 低木 / 1 木 */
static const unsigned char *gnd_xp[MAX_GND]; /* レーンの x[d] 表 (= 添字計算を避ける) */
static unsigned char gnd_ex[MAX_GND];     /* 爆発の残りフレーム (0 = 生きている) */

/* 種類 → 絵の起点 / 壊れないか (1 = 撃っても壊れない障害物) */
static const unsigned char gnd_kb[GND_KINDS]   = { OBJ_K_BUSH, OBJ_K_TREE };
static const unsigned char gnd_hard[GND_KINDS] = { 0, 1 };

/* 絵の段階を切り替える d。手前ほど大きい絵になる。 */
#define GND_STAGE_MID   16
#define GND_STAGE_NEAR  32

/* ---- 敵弾 ----
 * 0 自機狙い楕円弾 / 1 低空黒球弾 / 2 ボス火球。
 * 発射時だけ自機までの差を 32 で割り、以後は 8bit 加算だけで進める。
 *
 * 進み幅は **整数部 + 小数部 (1/256)** の 2 byte で持つ。整数だけだと差が
 * 32 の倍数でない限り切り捨てで狙いが大きく外れ (差 52 → 毎フレーム 1
 * → 32 フレーム後に 20 も手前)、自機狙いの弾が動かない自機にすら
 * 当たらなかった。小数部の桁上がりは 8bit の比較 1 つで拾う (16bit
 * 演算は使わない)。 */
#define MAX_EBUL       6
#define EBUL_FRAMES   52
#define EBUL_AIM       0
#define EBUL_LOW       1
#define EBUL_FIRE      2
static unsigned char ebul_hx[MAX_EBUL];
static unsigned char ebul_y[MAX_EBUL];
static unsigned char ebul_fx[MAX_EBUL];        /* 位置の小数部 (1/256) */
static unsigned char ebul_fy[MAX_EBUL];
static unsigned char ebul_dx[MAX_EBUL];        /* 進み幅の整数部 (2 の補数) */
static unsigned char ebul_dy[MAX_EBUL];
static unsigned char ebul_dxl[MAX_EBUL];       /* 進み幅の小数部 (1/256) */
static unsigned char ebul_dyl[MAX_EBUL];
static unsigned char ebul_age[MAX_EBUL];       /* 0 = 空き */
static unsigned char ebul_type[MAX_EBUL];
static unsigned char ebul_side;

/* ---- 蛇型ボス ----
 * 頭の位置を 64 フレームの輪に残し、胴体は一定間隔前の位置を読む。
 * 掛け算も三角関数も使わず、自然な蛇行になる。頭だけが弱点。 */
#define BOSS_SEGS      7
#define BOSS_HIST     64
#define BOSS_HP       12
unsigned char boss_on;
unsigned char boss_phase;
unsigned char boss_hp;
unsigned char boss_die;
static unsigned char boss_t;
static unsigned char boss_hx, boss_y, boss_hi, boss_flash;
static signed char boss_vx, boss_vy;
static unsigned char boss_hist_x[BOSS_HIST];
static unsigned char boss_hist_y[BOSS_HIST];
static const unsigned char boss_delay[BOSS_SEGS] = { 0,6,12,18,24,30,36 };

/* 消し込み表は自機 1 + OBJS 25 件ぶん。通常時は
 * 自弾 4 + 敵 8 + 地上物 6 + 敵弾 6 + 自機爆発 1 = 25。ボス開始時は敵と地上物を
 * 消すので、ボス 7 体を加えてもこの上限を下回る。 */
#define MAX_OBJS    25

/* ---- スクリプト (= 面の構成) ----
 * build/stage_script.h の stage_script[] を先頭から読む。
 * [符号, 次までのフレーム数] の並びで、符号は次のとおり (make_stage.py)。
 *   0x00-0x3F : 敵・岩の軌道番号 (ENM_PATH_xxx)。同じ軌道へ間隔を空けて
 *               何体も流し込めば、横一列や蛇のような編隊になる。
 *   0x40-0x4F : 敵弾 (0 楕円 / 1 低空黒球 / 2 ボス火球)
 *   0x50-0x5F : 蛇型ボスの局面 (0 接近 / 1 ループ / 2 退避 / 3 連射 / 4 至近)
 *   0x80-0xBF : 地上物。bit3-5 = 種類 (0 低木 / 1 木)、bit0-2 = レーン
 *   0xFE      : 何も出さずに待つ
 *   0xFF      : 面の終わり (先頭へ戻る)
 * スクリプトの中身は秒で書いた一覧から生成する。時刻や体数を直す時は
 * scripts/make_stage.py を直す。 */

static unsigned char bands[MAX_BANDS * 2];

/* デバッグ/計測用 (= 治具が build/horizonrunner.map からアドレスを引いて読む) */
unsigned int frame_cnt;
unsigned int hit_cnt;       /* 自機の被弾回数 (= 残機を 1 つ失った回数) */
unsigned int kill_cnt;      /* 撃破数 (敵・岩・低木) */
unsigned char pl_inv;       /* 自機の無敵 (点滅) の残りフレーム */
/* 被弾の演出。0 でない間は **敵も床も自弾も止め、自機の爆発だけを見せる**。
 * 0 になったら無敵 (点滅) のまま再開する。動きの中で被弾が埋もれて
 * しまわないよう、当たった瞬間を止めて見せるための仕掛けである。 */
unsigned char pl_die;
static unsigned char pl_ex_hx;   /* 爆発の位置 (2 dot 単位 / line) */
static unsigned char pl_ex_y;

static void delay_loop(unsigned int units)
{
    volatile unsigned int i;
    volatile unsigned char j;
    for (i = 0; i < units; i++) {
        for (j = 0; j < 200; j++) { /* spin */ }
    }
}

/* 敵弾を 1 発出す。sx/sy は枠内座標 (x は 2 dot 単位)。 */
static void enemy_bullet(unsigned char type, unsigned char sx,
                         unsigned char sy, unsigned int hx, unsigned char hy)
{
    unsigned char i;
    int dx, dy;
    for (i = 0; i < MAX_EBUL; i++) if (ebul_age[i] == 0) break;
    if (i >= MAX_EBUL) return;

    /* 32 フレームで自機の中心へ届く進み幅 = 差 / 32。整数部と小数部
     * (1/256) に分けて持つので、差 x 8 を 16bit で作り上下に割る
     * (発射時のみの 16bit 演算。以後は 8bit 加算だけ)。 */
    dx = ((int)((hx >> 1) + 6) - (int)sx) << 3;
    dy = ((int)(hy + 8) - (int)sy) << 3;
    ebul_dx[i]  = (unsigned char)(dx >> 8);
    ebul_dxl[i] = (unsigned char)dx;
    ebul_dy[i]  = (unsigned char)(dy >> 8);
    ebul_dyl[i] = (unsigned char)dy;
    if (type == EBUL_LOW) {             /* 地面すれすれを手前へ流す */
        ebul_dy[i] = 1;
        ebul_dyl[i] = 0;
    }
    ebul_hx[i] = sx;
    ebul_y[i] = sy;
    ebul_fx[i] = 0;
    ebul_fy[i] = 0;
    ebul_type[i] = type;
    ebul_age[i] = 1;
}

static void boss_set_phase(unsigned char p, unsigned char hz)
{
    unsigned char i;
    if (!boss_on) {
        boss_on = 1;
        boss_hp = BOSS_HP;
        boss_die = 0;
        boss_flash = 0;
        boss_hx = 84;
        boss_y = 4;
        boss_hi = 0;
        for (i = 0; i < BOSS_HIST; i++) {
            boss_hist_x[i] = boss_hx;
            boss_hist_y[i] = boss_y;
        }
        /* ボス戦へ通常物を持ち込まず、消し込み表の上限も守る。 */
        for (i = 0; i < MAX_ENM; i++) enm_step[i] = 0;
        for (i = 0; i < MAX_GND; i++) gnd_d[i] = 0;
        for (i = 0; i < MAX_EBUL; i++) ebul_age[i] = 0;
    }
    boss_phase = p;
    boss_t = 0;
    if (p == 1) { boss_vx = 3; boss_vy = 1; }
    /* 5 連射は左下から斜めに再接近しながら撃つ (スクリプト)。頭が動き続ける
     * ので胴が斜めに連なり、火球が胴に沿って数珠つなぎに見える。 */
    if (p == 3) { boss_hx = 28; boss_y = (hz > 6) ? (unsigned char)(hz - 6) : 0; }
    if (p == 4) { boss_vx = -4; boss_vy = 1; }
}

static void boss_update(unsigned int hx, unsigned char hy, unsigned char hz)
{
    unsigned char target;
    if (!boss_on) return;
    if (boss_die) {
        boss_die--;
        if (!boss_die) boss_on = 0;
        return;
    }
    boss_t++;
    switch (boss_phase) {
    case 0:                             /* 奥から接近しながら火球 */
        target = (hz > 12) ? (unsigned char)(hz - 12) : 4;
        if (boss_y < target && !(boss_t & 3)) boss_y++;
        if (boss_t & 16) boss_hx++; else boss_hx--;
        if (boss_hx < 56) boss_hx = 56;
        if (boss_hx > 112) boss_hx = 112;
        if ((boss_t & 63) == 1)
            enemy_bullet(EBUL_FIRE, boss_hx, boss_y, hx, hy);
        break;
    case 1:                             /* 輪を描く蛇行 (弾は撃たない) */
        boss_hx = (unsigned char)(boss_hx + boss_vx);
        boss_y  = (unsigned char)(boss_y + boss_vy);
        if (boss_hx < 20) boss_vx = 3; else if (boss_hx > 156) boss_vx = -3;
        if (boss_y < 5) boss_vy = 1; else if (boss_y > 42) boss_vy = -1;
        break;
    case 2:                             /* 画面奥へ退避しつつ火球 1 発 */
        if (boss_y > 7) boss_y--;
        if (boss_hx < 84) boss_hx++; else if (boss_hx > 84) boss_hx--;
        if (boss_t == 40)
            enemy_bullet(EBUL_FIRE, boss_hx, boss_y, hx, hy);
        break;
    case 3:                             /* 左下から斜めに寄りつつ 5 連射 */
        if (boss_hx < 150) boss_hx++;
        if (!(boss_t & 3) && boss_y > 4) boss_y--;
        if (boss_t == 1 || boss_t == 25 || boss_t == 49 ||
            boss_t == 73 || boss_t == 97)
            enemy_bullet(EBUL_FIRE, boss_hx, boss_y, hx, hy);
        break;
    default:                            /* 至近戦。自機の高さを左右へ大きく蛇行 */
        /* 頭は自機の正面 (y 20-44) を往復する (スクリプト)。範囲は「外れたら
         * 戻る向きにする」形で書き、範囲外から入った時に符号が毎フレーム
         * 反転して立ち往生しないようにする。 */
        boss_hx = (unsigned char)(boss_hx + boss_vx);
        boss_y  = (unsigned char)(boss_y + boss_vy);
        if (boss_hx < 8) boss_vx = 4; else if (boss_hx > 164) boss_vx = -4;
        if (boss_y < 20) boss_vy = 1; else if (boss_y > 44) boss_vy = -1;
        if ((boss_t & 31) == 1)
            enemy_bullet(EBUL_FIRE, boss_hx, boss_y, hx, hy);
        break;
    }
    boss_hi = (unsigned char)((boss_hi + 1) & (BOSS_HIST - 1));
    boss_hist_x[boss_hi] = boss_hx;
    boss_hist_y[boss_hi] = boss_y;
}

/* スクリプトの 1 項目を実体にする。空きが無ければその 1 体は諦める。 */
static void spawn(unsigned char code, unsigned int hx, unsigned char hy,
                  unsigned char hz)
{
    unsigned char bi;
    if (code == SC_WAIT) return;
    if (code & 0x80) {                      /* 地上物 */
        for (bi = 0; bi < MAX_GND; bi++) {
            if (gnd_d[bi] == 0) break;
        }
        if (bi < MAX_GND) {
            gnd_d[bi]    = GND_D0;
            gnd_ex[bi]   = 0;
            gnd_cnt[bi]  = gnd_dwell[GND_D0];
            gnd_kind[bi] = (unsigned char)((code >> 3) & 7);
            gnd_xp[bi]   = &gnd_x[gnd_xbase[code & 0x07]];
        }
    } else if (code < 0x40) {               /* 敵・岩 */
        for (bi = 0; bi < MAX_ENM; bi++) {
            if (enm_step[bi] == 0) break;
        }
        if (bi < MAX_ENM) {
            enm_step[bi] = 1;
            enm_ex[bi]   = 0;
            enm_path[bi] = code;
        }
    } else if (code < 0x50) {               /* 敵弾 */
        unsigned char type = (unsigned char)(code & 0x0F);
        unsigned char sx;
        ebul_side ^= 1;
        sx = ebul_side ? 42 : 142;
        if (type > EBUL_LOW) type = EBUL_FIRE;
        enemy_bullet(type, sx,
                     (type == EBUL_LOW) ? (unsigned char)(hz + 2)
                                        : (unsigned char)(hz - 8),
                     hx, hy);
    } else if (code < 0x60) {               /* ボスの局面 */
        boss_set_phase((unsigned char)(code & 0x0F), hz);
    }
}

/* ============================================================
 * 遊びの外枠 — タイトル / 残機 / 得点 / ゲームオーバー / 面クリア
 *
 * 文字は c_text.c が WRITE 命令で VRAM へ直に書く (サブ側は増やさない)。
 * 得点と残機は **描画枠 (384x96) の外**、カラープレーン (緑) の上に置く。情景
 * コマンドは枠の中しか触らないので、一度書けば消えない。**値が変わった
 * フレームだけ**書き直す (毎フレームの仕事を増やさないため)。
 * ============================================================ */

/* 表示の位置 (画面全体の byte / line。枠は 16-63 byte / 52-147 line) */
#define HUD_Y          40           /* 枠の上、8 line ぶんの帯 */
#define HUD_SCORE_X    16
#define HUD_VAL_X      22           /* 得点の数字 6 桁 */
#define HUD_REST_X     56
#define HUD_LIFE_X     61           /* 残機の数字 1 桁 */
/* 面クリアの幕は枠の中の空へ出す。情景コマンドが消しに来るのは地平線
 * (枠内 20 line 以降) から下だけなので、ここは塗り潰されない。 */
#define BANNER_X       29
#define BANNER_Y       (FRAME_Y_LINE + 6)
#define BANNER_W       22           /* 横 2 倍の 11 字 = 22 byte */

#define LIVES_START    3
#define OVER_DELAY     (PL_DIE_FRAMES + 20)  /* 最後の 1 機が爆ぜてから幕まで */
#define CLEAR_SHOW     150          /* 面クリアの幕を出しておく長さ */

/* 書き直しの予約 (= 次のフレームの頭でまとめて送る)。得点と残機を分けて
 * あるのは、撃破のたびに残機まで書き直すとサブの呼び出しが 1 回増えて
 * 撃破の多い区間で 1fps 落ちるためである。 */
#define HUD_D_SCORE    0x01         /* 得点の数字 6 桁 */
#define HUD_D_LIFE     0x08         /* 残機の数字 1 桁 */
#define HUD_D_VAL      (HUD_D_SCORE | HUD_D_LIFE)
#define HUD_D_BANNER   0x02         /* 面クリアの幕を出す */
#define HUD_D_UNBANNER 0x04         /* 面クリアの幕を消す */

/* いまの場面。**撮影治具がリンクマップから引いて「場面を見て撮る」**
 * ために外へ出してある (フレーム番号の直書きを避けるため)。 */
#define GS_BOOT    0        /* 起動中 (= RAM の初期値と同じ値にしてある。
                             *   crt0 は bss を消さないので、まだ main に
                             *   入っていない間の読み値がこれになる) */
#define GS_TITLE   1
#define GS_CLEAR   2
#define GS_OVER    3
#define GS_PLAY    4
unsigned char game_state;
unsigned char lives;                /* 残機 (0 = 後が無い) */
static unsigned char hud_dirty;
static unsigned char over_t;        /* 残機切れ後の秒読み (0 = 継続) */
static unsigned char clear_t;       /* 面クリアの幕の残りフレーム */
/* 得点は 10 進の桁で持つ (掛け算も割り算もしないため)。[0] が 10 万の位。 */
static unsigned char sc_dig[6];
static char sc_str[7];

/* 桁 place (0 = 最上位) に amount (0-9) を足し、繰り上がりを上へ伝える。
 * 実行時の乗除算は無い。 */
static void score_add(unsigned char place, unsigned char amount)
{
    signed char i = (signed char)place;
    unsigned char v = amount;
    while (v && i >= 0) {
        unsigned char d = (unsigned char)(sc_dig[i] + v);
        if (d >= 10) { sc_dig[i] = (unsigned char)(d - 10); v = 1; }
        else         { sc_dig[i] = d;                      v = 0; }
        i--;
    }
    hud_dirty |= HUD_D_SCORE;
}

/* 得点を "000000" の並びにする (タイトル・幕・HUD で使い回す) */
static const char *score_text(void)
{
    unsigned char i;
    for (i = 0; i < 6; i++) sc_str[i] = (char)('0' + sc_dig[i]);
    sc_str[6] = '\0';
    return sc_str;
}

/* 被弾。**画面を止めて自機の爆発を見せ、無敵で再開する**までを 1 か所で
 * 決める。残機を 1 つ減らし、尽きたら幕引きの秒読みを始める。
 *
 * 無敵の残りフレーム (pl_inv) はここで一度に積む。演出の間は自機を描かない
 * = 減らさないので、**止まっている間は目減りせず、再開してから 3 秒**残る。 */
static void player_hit(unsigned int hx, unsigned char hy)
{
    hit_cnt++;
    pl_inv   = PL_INV_FRAMES;
    pl_die   = PL_DIE_FRAMES;
    pl_ex_hx = (unsigned char)(hx >> 1);
    pl_ex_y  = (unsigned char)(hy + 3);
    input_dir_clear();          /* 方向のラッチを解く (= 復帰後に流されない) */
    sfx_destroy();
    if (lives) {
        lives--;
        hud_dirty |= HUD_D_LIFE;
    }
    if (lives == 0 && over_t == 0) over_t = OVER_DELAY;
}

/* 1 フレーム待つ (= タイトルと幕でだけ使う。面の中は main の中に直に
 * 書いてある同じ待ちを使う)。 */
static void wait_frame(void)
{
    unsigned int guard = 0;
    while (timer_get() < FRAME_TARGET && guard < PACE_SAFETY_CAP) guard++;
    timer_consume(FRAME_TARGET);
}

/* 予約された書き直しを送る。**非同期の描画とかち合わないよう**、必ず
 * sub_sync() の後に呼ぶこと。 */
static void hud_service(void)
{
    if (!hud_dirty) return;
    sub_sync();
    if (hud_dirty & HUD_D_SCORE) {
        text_put(TEXT_PLANE_G, HUD_VAL_X, HUD_Y, score_text());
    }
    if (hud_dirty & HUD_D_LIFE) {
        char rest[2];
        rest[0] = (char)('0' + lives);
        rest[1] = '\0';
        text_put(TEXT_PLANE_G, HUD_LIFE_X, HUD_Y, rest);
    }
    if (hud_dirty & HUD_D_BANNER) {
        text_put_wide(TEXT_PLANE_G, BANNER_X, BANNER_Y, "STAGE CLEAR");
    }
    if (hud_dirty & HUD_D_UNBANNER) {
        text_erase(TEXT_PLANE_G, BANNER_X, BANNER_Y, BANNER_W, 8);
    }
    hud_dirty = 0;
    sub_flush();
}

/* 何か押されたか (BREAK か RETURN)。ジョイスティックは本作のビルドに
 * 読み取りが入っていないので見ない (BREAK 側で拾える)。 */
static unsigned char start_pressed(void)
{
    if (break_shots()) return 1;
    if (input_edges() & IN_START) return 1;
    return 0;
}

/* ---- タイトル画面 ---- */
static void title_screen(void)
{
    unsigned char blink = 0;
    unsigned char shown = 0;

    game_state = GS_TITLE;
    sub_sync();
    sub_cls();                      /* 3 面とも消す */
    sub_page(0);
    sub_flush();
    palette_page(0);

    text_put_wide(TEXT_PLANE_0, 26, 56, "HORIZON RUNNER");
    text_put(TEXT_PLANE_G, 29, 96,  "MOVE  TENKEY 12346789");
    text_put(TEXT_PLANE_G, 32, 110, "SHOT  BREAK KEY");
    sub_flush();

    input_edges();                  /* 溜まっている押下を捨てる */
    break_shots();

    for (;;) {
        wait_frame();
        sound_tick();
        if (start_pressed()) break;
        /* 「押してください」だけ点滅させる (1 秒ごと) */
        if ((blink & 31) == 0) {
            shown ^= 1;
            if (shown) {
                text_put(TEXT_PLANE_G, 28, 140, "PUSH BREAK KEY TO START");
            } else {
                text_erase(TEXT_PLANE_G, 28, 140, 23, 8);
            }
            sub_flush();
        }
        blink++;
    }
}

/* ---- ゲームオーバーの幕 ---- */
static void gameover_screen(void)
{
    unsigned char t;

    game_state = GS_OVER;
    sub_sync();
    sub_cls();
    sub_page(0);
    sub_flush();
    palette_page(0);

    text_put_wide(TEXT_PLANE_0, 31, 80, "GAME OVER");
    text_put(TEXT_PLANE_G, 34, 108, "SCORE ");
    text_put(TEXT_PLANE_G, 40, 108, score_text());
    sub_flush();

    input_edges();
    break_shots();
    for (t = 0; t < 180; t++) {     /* 約 3 秒。押せば飛ばせる */
        wait_frame();
        sound_tick();
        if (t > 30 && start_pressed()) break;
    }
}

/* ---- 面を 1 回遊ぶ (= 残機が尽きるまで戻らない) ---- */
static void play(void)
{
    unsigned int hx;            /* 自機の横位置 (枠内 dot) */
    unsigned char hy;           /* 自機の縦位置 (枠内 line) */
    unsigned char hz;           /* 地平線の枠内 line */
    unsigned char hz_prev;      /* カラープレーンにいま描いてある地平線 */
    unsigned char zoff;         /* 奥行きスクロールの位相 */
    unsigned char sx, sy, ph;   /* 自機の VRAM 位置と横位相 */
    unsigned char page;         /* これから描く面 (0 = B plane / 1 = R plane) */
    unsigned char bi;           /* 自弾 / 敵のスロット番号 */
    unsigned char sc_t;         /* スクリプトの次の項目までのフレーム数 */
    unsigned int  sc_i;         /* スクリプトのどこを読んでいるか (項目 x 2) */
    unsigned char *op;          /* OBJS 命令の書込先 */
    unsigned char nobj;         /* この面に描く絵の個数 */
    unsigned char nbul;         /* 今フレーム生きている自弾の数 (0 なら判定を飛ばす) */
    unsigned char plx0, plx1, ply0, ply1;   /* 自機の当たり枠 (2 dot 単位 / line) */

    hx = 176;                   /* 枠の横中央あたり */
    hy = HY_MID;
    hz = HZ_MID;
    hz_prev = 0;                /* 起動直後のカラープレーンは真っ黒 */
    zoff = 0;
    page = 0;
    sc_t = 0;
    sc_i = 0;
    for (bi = 0; bi < MAX_BUL; bi++) { bul_life[bi] = 0; bul_pop[bi] = 0; }
    for (bi = 0; bi < MAX_ENM; bi++) { enm_step[bi] = 0; enm_ex[bi] = 0; }
    for (bi = 0; bi < MAX_GND; bi++) { gnd_d[bi] = 0; gnd_ex[bi] = 0; }
    for (bi = 0; bi < MAX_EBUL; bi++) ebul_age[bi] = 0;
    ebul_side = 0;
    boss_on = 0;
    boss_phase = 0;
    boss_hp = 0;
    boss_die = 0;
    frame_cnt = 0;
    hit_cnt = 0;
    kill_cnt = 0;
    pl_inv = 0;
    pl_die = 0;
    lives   = LIVES_START;
    over_t  = 0;
    clear_t = 0;
    hud_dirty = 0;
    for (bi = 0; bi < 6; bi++) sc_dig[bi] = 0;

    sub_cls();                  /* 3 面とも消す */
    sub_page(0);
    sub_flush();
    palette_page(0);

    /* 得点と残機の見出し (= 変わらない字) は面の頭で 1 回だけ書く。
     * 情景コマンドは枠の中しか触らないので、以後は消えない。 */
    text_put(TEXT_PLANE_G, HUD_SCORE_X, HUD_Y, "SCORE");
    text_put(TEXT_PLANE_G, HUD_REST_X,  HUD_Y, "REST");
    sub_flush();
    hud_dirty = HUD_D_VAL;
    hud_service();
    game_state = GS_PLAY;

    for (;;) {
        unsigned char d, nb, rows, dd, run, par, npar, clr_from, clr_lines;
        unsigned char frz;      /* 1 = 被弾の演出中 (敵も床も自弾も止める) */

        /* ---- フレームペーシング (deadline、持ち越し式) ---- */
        {
            unsigned int guard = 0;
            while (timer_get() < FRAME_TARGET && guard < PACE_SAFETY_CAP) {
                guard++;
            }
            timer_consume(FRAME_TARGET);
        }
        frame_cnt++;
        sound_tick();

        /* 予約された書き直し (得点・残機・面クリアの幕) を送る。値が
         * 変わったフレームだけサブを 1 回余分に呼ぶ。 */
        hud_service();
        if (over_t) {           /* 残機切れ。爆発を見せ切ってから幕へ */
            over_t--;
            if (over_t == 0) return;
        }
        /* 被弾の演出。**この間は敵も床も自弾も止め、自機の爆発だけを
         * 動かす** (どこで当たったのかを見せるため)。無敵の残りは自機を
         * 描く時にしか減らないので、止まっている間は目減りしない。 */
        frz = 0;
        if (pl_die) {
            pl_die--;
            frz = 1;
        }
        if (clear_t) {
            clear_t--;
            if (clear_t == 0) {
                hud_dirty |= HUD_D_UNBANNER;
                game_state = GS_PLAY;
            }
        }

        /* ---- 入力 (方向はラッチ保持。演出中は動かさない) ---- */
        d = frz ? 0 : input_dir();
        if (d & IN_UP)    { hy = (hy > HY_MIN + VSTEP) ? (unsigned char)(hy - VSTEP) : HY_MIN; }
        if (d & IN_DOWN)  { hy = (hy < HY_MAX - VSTEP) ? (unsigned char)(hy + VSTEP) : HY_MAX; }
        if (d & IN_LEFT)  { hx = (hx > HX_MIN + HSTEP) ? (hx - HSTEP) : HX_MIN; }
        if (d & IN_RIGHT) { hx = (hx < HX_MAX - HSTEP) ? (hx + HSTEP) : HX_MAX; }

        /* ---- カメラのピッチ (= 地平線の上下)。必ず偶数行へ丸める ---- */
        {
            signed char t = (signed char)((signed char)HY_MID - (signed char)hy);
            signed char h = (signed char)(HZ_MID + (t >> 2));
            if (h < HZ_MIN) h = HZ_MIN;
            if (h > HZ_MAX) h = HZ_MAX;
            hz = (unsigned char)((unsigned char)h & 0xFE);
        }

        /* ---- 前進 (演出中は床も止める) ---- */
        if (!frz) zoff = (unsigned char)(zoff + ZSPEED);

        /* ---- 床の帯を組み立てる ----
         * ★ ここは 1 フレームに 60 回以上まわる。**乗除算を絶対に置かない**
         *   こと。以前ここに 16bit の掛け算を 1 つ置いただけで 61fps が
         *   40fps を割った (CMOC の乗除算は実行時ルーチン呼出になる)。 */
        rows = (unsigned char)((FRAME_H_LINES - 1 - hz) >> 1);
        nb = 0;
        if (rows) {
            unsigned char far;
            far = (rows < (unsigned char)((FLR_D_ALIAS - 1) >> 1))
                      ? rows : (unsigned char)((FLR_D_ALIAS - 1) >> 1);
            bands[0] = far;             /* 遠景は 1 本の黒い帯にまとめる */
            bands[1] = FLR_FAR_PAT;
            nb = 1;
            if (rows > far) {
                run = (unsigned char)(far + 1);
                /* 遠近表 z_lo[] は物理 line 単位なので、論理行を 2 倍して引く */
                par = (unsigned char)((z_lo[run << 1] + zoff) & 0x80);
                for (dd = (unsigned char)(run + 1); dd <= rows; dd++) {
                    npar = (unsigned char)((z_lo[dd << 1] + zoff) & 0x80);
                    if (npar != par) {
                        if (nb < MAX_BANDS) {
                            bands[nb * 2]     = (unsigned char)(dd - run);
                            bands[nb * 2 + 1] = par ? FLR_SOLID_LIGHT
                                                    : FLR_SOLID_DARK;
                            nb++;
                        }
                        par = npar;
                        run = dd;
                    }
                }
                if (nb < MAX_BANDS) {
                    bands[nb * 2]     = (unsigned char)(rows - run + 1);
                    bands[nb * 2 + 1] = par ? FLR_SOLID_LIGHT : FLR_SOLID_DARK;
                    nb++;
                }
            }
        }

        /* ---- 空へ戻す行 (= 地平線が下がった分) ----
         * 床はカラープレーン 1 枚なので履歴は 1 つで足りる。 */
        if (hz > hz_prev) {
            clr_from  = hz_prev;
            clr_lines = (unsigned char)((hz - hz_prev) >> 1);
        } else {
            clr_from  = 0;
            clr_lines = 0;
        }
        hz_prev = hz;

        /* ---- 発射 (BREAK。押しっぱなしでオート連射) ---- */
        {
            unsigned char n = break_shots();
            if (frz) n = 0;         /* 演出中は撃たない (押下は捨てる) */
            while (n) {
                n--;
                for (bi = 0; bi < MAX_BUL; bi++) {
                    if (bul_life[bi] == 0) break;
                }
                if (bi >= MAX_BUL) break;       /* 空きが無い */
                {
                    /* 発射時だけ 16 分割の割り算をする (= 4 bit 右シフト)。
                     * 以後は毎フレーム足すだけで収束点へ向かう。
                     * ★ 差を 8bit で取ってはならない。砲口の x は最大 189 で
                     *   signed char に収まらず、符号が反転して弾が明後日へ
                     *   飛ぶ。差の計算だけは 16bit で行う (発射時のみ)。 */
                    unsigned char gx = (unsigned char)((hx + 26) >> 1);
                    unsigned char gy = (unsigned char)(hy + 6);
                    bul_hx[bi] = gx;
                    bul_y[bi]  = gy;
                    bul_dx[bi] = (unsigned char)(((int)VANISH_HX - (int)gx) >> 4);
                    bul_dy[bi] = (unsigned char)(((int)VANISH_Y - (int)gy) >> 4);
                    bul_life[bi] = BUL_LIFE;
                    bul_pop[bi]  = 0;
                    sfx_shot();
                }
            }
        }

        /* ---- 湧き (= スクリプトどおりに 1 体ずつ流す) ----
         * 間隔 0 の項目が続けば同じフレームに複数を出す (最大 4 つまで
         * 見て、残りは次のフレームへ持ち越す = 1 フレームの仕事を抑える)。
         * 間隔 g の項目は前の項目のちょうど g フレーム後に出る (= 先に
         * 減らしてから 0 を見る。後で減らすと 1 項目ごとに 1 フレーム
         * ずつ遅れが溜まり、面の終わりでは 2 秒以上ずれる)。
         * 被弾の演出中 (frz) はスクリプトごと止める。 */
        if (!frz && sc_t) sc_t--;
        if (!frz && sc_t == 0) {
            unsigned char guard = 4;
            for (;;) {
                unsigned char code = stage_script[sc_i];
                if (code == SC_END) {       /* 面の終わり → 先頭へ戻る */
                    /* ボスが生きている間は終端に留まり、通常編隊を重ねない。 */
                    if (boss_on) { sc_t = 255; break; }
                    sc_i = 0;
                    code = stage_script[0];
                }
                spawn(code, hx, hy, hz);
                sc_t = stage_script[sc_i + 1];
                sc_i += 2;
                if (sc_t || --guard == 0) break;
            }
        }

        if (!frz) {   /* ボスを撃破し切った瞬間が面クリア (= 爆発が終わった時) */
            unsigned char was = boss_on;
            boss_update(hx, hy, hz);
            if (was && !boss_on) {
                score_add(2, 5);            /* 5000 点 */
                clear_t = CLEAR_SHOW;
                game_state = GS_CLEAR;
                hud_dirty |= HUD_D_BANNER;
            }
        }

        /* ---- 自機の VRAM 位置 ---- */
        sx = (unsigned char)(FRAME_X_BYTE + (unsigned char)(hx >> 3));
        ph = (unsigned char)(hx & 7);
        sy = (unsigned char)(FRAME_Y_LINE + hy);

        /* ---- 裏の面へ描き切る ----
         * 面の選択 (= サブが前回の絵を消す) → 情景 → 自機 → 絵の一覧
         * (地上物 → 自弾 → 敵・岩)。
         * ★ 絵は全部 1 つの OBJS 命令に詰める。キューは 108 byte しか無く、
         *   あふれてサブを 2 回呼ぶと往復だけで約 2ms 失う (実測 57→51fps)。
         *   1 個 3 byte なので、自弾 4 + 敵 8 + 地上物 8 = 20 個で 62 byte。 */
        sub_page(page);
        sub_scene(clr_from, clr_lines, hz, bands, nb);
        /* 演出中は自機を描かない (爆発だけを見せる)。復帰後の無敵中は
         * 2 フレームおきに描かない (= 点滅)。**無敵の残りはここでしか
         * 減らない**ので、止まっている間は目減りしない。 */
        if (frz) {
            /* 自機は消えている (下で爆発を描く) */
        } else if (pl_inv) {
            pl_inv--;
            if (!(pl_inv & 2)) sub_sprite(sx, sy, ph);
        } else {
            sub_sprite(sx, sy, ph);
        }
        op = sub_objs_open(MAX_OBJS);
        nobj = 0;

        /* ---- 自機の当たり枠 (2 dot 単位 / line。絵よりひと回り小さい) ---- */
        plx0 = (unsigned char)((hx >> 1) + PL_INSET_X);
        plx1 = (unsigned char)((hx >> 1) + (SPR_W_DOTS / 2) - PL_INSET_X);
        ply0 = (unsigned char)(hy + PL_INSET_Y);
        ply1 = (unsigned char)(hy + SPR_H_LINES - PL_INSET_Y);

        /* ---- 自弾を進める (描くのは地上物の後。判定に使う矩形と段階を
         *      ここで 1 回だけ求めて憶える) ---- */
        nbul = 0;
        for (bi = 0; frz == 0 && bi < MAX_BUL; bi++) {
            unsigned char k, lf;
            lf = bul_life[bi];
            if (lf == 0) continue;
            if (bul_pop[bi]) {              /* 木に当たって爆ぜている弾は動かない */
                bul_pop[bi]--;
                if (bul_pop[bi] == 0) bul_life[bi] = 0;
                continue;
            }
            bul_hx[bi] = (unsigned char)(bul_hx[bi] + bul_dx[bi]);
            bul_y[bi]  = (unsigned char)(bul_y[bi] + bul_dy[bi]);
            lf--;
            bul_life[bi] = lf;
            if (lf == 0) continue;
            /* 枠外へ出た弾は消す。8bit で回っているので、負へ回り込んだ弾は
             * 大きな値になり、この検査にそのまま引っ掛かる。 */
            if (bul_y[bi] > (unsigned char)(FRAME_H_LINES - 4) ||
                bul_hx[bi] > (unsigned char)(FRAME_W_DOTS / 2 - 8)) {
                bul_life[bi] = 0;
                continue;
            }
            /* 遠ざかるほど小さく。奥行きの手掛かりはこの大きさだけなので、
             * 3 段階でも十分に「奥へ飛んだ」と読める。絵は 15-17。 */
            if (lf >= 12)      { k = OBJ_K_BULLET;     bul_st[bi] = 2; }
            else if (lf >= 7)  { k = OBJ_K_BULLET + 1; bul_st[bi] = 1; }
            else               { k = OBJ_K_BULLET + 2; bul_st[bi] = 0; }
            bul_k[bi]  = k;
            bul_x1[bi] = (unsigned char)(bul_hx[bi] + (obj_w[k] << 2));
            bul_y1[bi] = (unsigned char)(bul_y[bi] + obj_h[k]);
            nbul++;
        }

        /* ---- 地上物を進めて描く ----
         * 残りフレームを 1 減らし、0 になったら d を 1 進めて次の滞在数を
         * 表から取る。滞在 0 の d (= 手前で 1 フレームに 2 line 以上進む
         * 区間) は続けて飛ばす。先に描くので、後の自弾・敵が必ず手前。
         * 爆発中 (gnd_ex) も床と一緒に流し、絵だけ爆発に差し替える。 */
        for (bi = 0; bi < MAX_GND; bi++) {
            unsigned char gd, gc, k, gy, gx, hw, lim, st, ex;
            gd = gnd_d[bi];
            if (gd == 0) continue;
            ex = gnd_ex[bi];
            if (!frz) {             /* 演出中は歩も爆発も止める */
                if (ex) {
                    ex--;
                    if (ex == 0) { gnd_d[bi] = 0; continue; }
                    gnd_ex[bi] = ex;
                }
                gc = (unsigned char)(gnd_cnt[bi] - 1);
                while (gc == 0) {
                    gd++;
                    if (gd > GND_D1) break;
                    gc = gnd_dwell[gd];
                }
                if (gd > GND_D1) { gnd_d[bi] = 0; continue; }
                gnd_d[bi]   = gd;
                gnd_cnt[bi] = gc;
            }
            /* 床は hz から 1 行おきの偶数位相へ描く。gd が奇数のままでは
             * 絵の底が間引いた黒い行へ来て、特に背の低い低木が 1 line
             * 浮いて見える。接地点を床と同じ位相へ丸める。 */
            gy = (unsigned char)(hz + (gd & 0xFE));      /* 絵の底 = 描いた床 */
            if (gy >= FRAME_H_LINES) { gnd_d[bi] = 0; continue; }
            st = (gd < GND_STAGE_MID) ? 0 : ((gd < GND_STAGE_NEAR) ? 1 : 2);
            k = (unsigned char)(st + (ex ? EXP_KIND(ex) : gnd_kb[gnd_kind[bi]]));
            gy = (unsigned char)(gy - obj_h[k]);        /* 上端 */
            gx = gnd_xp[bi][gd];                        /* 絵の中心 */
            hw = obj_hw[k];
            gx = (gx > hw) ? (unsigned char)(gx - hw) : 0;
            lim = (unsigned char)(FRAME_W_DOTS / 2 - (obj_w[k] << 2));
            if (gx > lim) gx = lim;
            if (ex == 0 && !frz) {
                unsigned char x1 = (unsigned char)(gx + (obj_w[k] << 2));
                unsigned char y1 = (unsigned char)(gy + obj_h[k]);
                unsigned char hard = gnd_hard[gnd_kind[bi]];
                unsigned char bj;
                /* 自弾との判定 (段階が同じか 1 つ手前 → 矩形の重なり) */
                if (nbul) {
                    for (bj = 0; bj < MAX_BUL; bj++) {
                        if (bul_life[bj] == 0 || bul_pop[bj]) continue;
                        if (bul_st[bj] != st && bul_st[bj] != (unsigned char)(st + 1)) continue;
                        if (bul_hx[bj] >= x1 || gx >= bul_x1[bj]) continue;
                        if (bul_y[bj]  >= y1 || gy >= bul_y1[bj]) continue;
                        if (hard) {                 /* 木: 弾だけ爆ぜて消える */
                            bul_pop[bj] = POP_LEN;
                        } else {                    /* 低木: 壊れて爆発 */
                            bul_life[bj] = 0;
                            ex = EXP_LEN;
                            kill_cnt++;
                            score_add(4, 5);        /* 50 点 */
                            sfx_destroy();
                        }
                        break;
                    }
                }
                /* 自機との接触 (最も手前の段階だけ) */
                if (st == 2 && pl_inv == 0 &&
                    plx0 < x1 && gx < plx1 && ply0 < y1 && gy < ply1) {
                    player_hit(hx, hy);
                    if (!hard) ex = EXP_LEN;
                }
                if (ex) {                           /* 今フレームから爆発の絵へ */
                    gnd_ex[bi] = ex;
                    k = (unsigned char)(st + OBJ_K_EXPL_A);
                    gy = (unsigned char)(hz + (gd & 0xFE) - obj_h[k]);
                }
            }
            op[0] = (unsigned char)(FRAME_X_BYTE + (gx >> 2));
            op[1] = (unsigned char)(FRAME_Y_LINE + gy);
            op[2] = (unsigned char)(k | ((gx & 3) << 6));
            op += 3;
            nobj++;
        }

        /* ---- 自弾を描く (進めるのは上で済ませた) ---- */
        for (bi = 0; bi < MAX_BUL; bi++) {
            unsigned char k;
            if (bul_life[bi] == 0) continue;
            /* 木に当たった弾は、その場で小さく爆ぜる絵 (遠の爆発 A) */
            k = bul_pop[bi] ? OBJ_K_EXPL_A : bul_k[bi];
            op[0] = (unsigned char)(FRAME_X_BYTE + (bul_hx[bi] >> 2));
            op[1] = (unsigned char)(FRAME_Y_LINE + bul_y[bi]);
            op[2] = (unsigned char)(k | ((bul_hx[bi] & 3) << 6));
            op += 3;
            nobj++;
        }

        /* ---- 敵と岩を進めて描く ----
         * 歩の間隔は軌道ごと (毎フレーム / 2 フレームに 1 歩)。描画は
         * 毎フレーム行うので、動きが飛んで見えることはない。
         * 爆発中 (enm_ex) は歩を止め、その場に爆発の絵を出して消える。 */
        for (bi = 0; bi < MAX_ENM; bi++) {
            unsigned char st, idx, k, ey, exh, pth, sg, ex;
            unsigned int  base;
            st = enm_step[bi];
            if (st == 0) continue;
            ex = enm_ex[bi];
            if (ex && !frz) {       /* 演出中は爆発のこまも止める */
                ex--;
                if (ex == 0) { enm_step[bi] = 0; continue; }
                enm_ex[bi] = ex;
            }
            pth  = enm_path[bi];
            idx  = (unsigned char)(st - 1);
            base = enm_pbase[pth] + idx;
            /* 絵の段階 (遠/中/近) も表に焼いてある。種類の起点は軌道ごと
             * (敵 A / 敵 C / 岩)。 */
            sg = enm_pk[base];
            k  = (unsigned char)(sg + (ex ? EXP_KIND(ex) : enm_kbase[pth]));
            ey = (unsigned char)(hz + enm_pd[base]);   /* 上は負 (2 の補数) */
            /* 枠の外 (下端を割った / 上へ抜けた) なら通り過ぎたとして消す。
             * 8bit で回っているので、上へ抜けた負の値も大きな値になって
             * この 1 つの検査に引っ掛かる。 */
            if ((unsigned char)(ey + obj_h[k]) >= FRAME_H_LINES || ey >= FRAME_H_LINES) {
                enm_step[bi] = 0;
                continue;
            }
            exh = enm_px[base];
            /* 255 = 左端の外 (軌道表が焼いた印)。描かず・当てず、歩だけ
             * 進める (以前は 0 へ寄せていたので左端に張り付いて見えた)。 */
            if (exh == 255) {
                if (ex == 0 && !frz && (frame_cnt & enm_ptick[pth]) == 0) {
                    st++;
                    enm_step[bi] = (st > enm_plen[pth]) ? 0 : st;
                }
                continue;
            }
            {   /* 枠の右端からはみ出さないよう寄せる */
                unsigned char lim =
                    (unsigned char)(FRAME_W_DOTS / 2 - (obj_w[k] << 2));
                if (exh > lim) exh = lim;
            }
            if (ex == 0 && !frz) {
                unsigned char x1 = (unsigned char)(exh + (obj_w[k] << 2));
                unsigned char y1 = (unsigned char)(ey + obj_h[k]);
                unsigned char bj;
                /* 自弾との判定 (段階が同じか 1 つ手前 → 矩形の重なり) */
                if (nbul) {
                    for (bj = 0; bj < MAX_BUL; bj++) {
                        if (bul_life[bj] == 0 || bul_pop[bj]) continue;
                        if (bul_st[bj] != sg && bul_st[bj] != (unsigned char)(sg + 1)) continue;
                        if (bul_hx[bj] >= x1 || exh >= bul_x1[bj]) continue;
                        if (bul_y[bj]  >= y1 || ey  >= bul_y1[bj]) continue;
                        bul_life[bj] = 0;
                        ex = EXP_LEN;
                        kill_cnt++;
                        score_add(3, 2);        /* 200 点 */
                        sfx_destroy();
                        break;
                    }
                }
                /* 自機との接触 (最も手前の段階だけ) */
                if (sg == 2 && pl_inv == 0 &&
                    plx0 < x1 && exh < plx1 && ply0 < y1 && ey < ply1) {
                    player_hit(hx, hy);
                    ex = EXP_LEN;
                }
                if (ex) {                           /* 今フレームから爆発の絵へ */
                    enm_ex[bi] = ex;
                    k = (unsigned char)(sg + OBJ_K_EXPL_A);
                }
            }
            op[0] = (unsigned char)(FRAME_X_BYTE + (exh >> 2));
            op[1] = (unsigned char)(FRAME_Y_LINE + ey);
            op[2] = (unsigned char)(k | ((exh & 3) << 6));
            op += 3;
            nobj++;
            if (ex == 0 && !frz && (frame_cnt & enm_ptick[pth]) == 0) {
                st++;
                enm_step[bi] = (st > enm_plen[pth]) ? 0 : st;
            }
        }

        /* ---- 蛇型ボス ----
         * 履歴を尾から頭の順に貼る。頭だけが自弾で傷付き、胴体への弾は
         * 通り抜ける。撃破時は全節を同時に爆発へ差し替える。 */
        if (boss_on) {
            unsigned char si, bst;
            bst = (boss_phase == 0)
                    ? ((boss_t < 80) ? 0 : ((boss_t < 160) ? 1 : 2))
                    : ((boss_phase == 2) ? 1 : 2);
            if (boss_flash && !frz) boss_flash--;
            si = BOSS_SEGS;
            while (si) {
                unsigned char idx, bx, by, k, x1, y1;
                si--;
                idx = (unsigned char)((boss_hi - boss_delay[si]) & (BOSS_HIST - 1));
                bx = boss_hist_x[idx];
                by = boss_hist_y[idx];
                if (boss_die) {     /* 撃破: 全節が芯と破片を 4 フレームおきに交互 */
                    k = (unsigned char)(((boss_die & 4) ? OBJ_K_EXPL_A
                                                        : OBJ_K_EXPL_B) + bst);
                } else if (si == 0) {
                    k = (unsigned char)((boss_flash ? OBJ_K_EXPL_A : OBJ_K_FLYER) + bst);
                } else {
                    k = (unsigned char)(OBJ_K_ORB + bst);
                }
                x1 = (unsigned char)(bx + (obj_w[k] << 2));
                y1 = (unsigned char)(by + obj_h[k]);

                if (!boss_die && si == 0 && nbul) {
                    unsigned char bj;
                    for (bj = 0; bj < MAX_BUL; bj++) {
                        if (bul_life[bj] == 0 || bul_pop[bj]) continue;
                        if (bul_hx[bj] >= x1 || bx >= bul_x1[bj]) continue;
                        if (bul_y[bj] >= y1 || by >= bul_y1[bj]) continue;
                        bul_life[bj] = 0;
                        boss_flash = 3;
                        sfx_destroy();
                        if (boss_hp) boss_hp--;
                        if (!boss_hp) {
                            unsigned char ei;
                            boss_die = 48;
                            kill_cnt++;
                            for (ei = 0; ei < MAX_EBUL; ei++) ebul_age[ei] = 0;
                        }
                        break;
                    }
                }
                if (!boss_die && bst == 2 && pl_inv == 0 &&
                    plx0 < x1 && bx < plx1 && ply0 < y1 && by < ply1) {
                    player_hit(hx, hy);
                }
                op[0] = (unsigned char)(FRAME_X_BYTE + (bx >> 2));
                op[1] = (unsigned char)(FRAME_Y_LINE + by);
                op[2] = (unsigned char)(k | ((bx & 3) << 6));
                op += 3;
                nobj++;
            }
        }

        /* ---- 敵弾 3 種 ---- */
        for (bi = 0; bi < MAX_EBUL; bi++) {
            unsigned char age, st, k, x1, y1, lim;
            age = ebul_age[bi];
            if (!age) continue;
            if (!frz) {             /* 演出中はその場に止める */
                {   /* 小数部を足し、桁上がりを整数部へ (8bit の比較 1 つ) */
                    unsigned char f = (unsigned char)(ebul_fx[bi] + ebul_dxl[bi]);
                    if (f < ebul_fx[bi]) ebul_hx[bi]++;
                    ebul_fx[bi] = f;
                    f = (unsigned char)(ebul_fy[bi] + ebul_dyl[bi]);
                    if (f < ebul_fy[bi]) ebul_y[bi]++;
                    ebul_fy[bi] = f;
                }
                ebul_hx[bi] = (unsigned char)(ebul_hx[bi] + ebul_dx[bi]);
                ebul_y[bi] = (unsigned char)(ebul_y[bi] + ebul_dy[bi]);
                age++;
                if (age > EBUL_FRAMES || ebul_hx[bi] > 184 ||
                    ebul_y[bi] > (FRAME_H_LINES - 3)) {
                    ebul_age[bi] = 0;
                    continue;
                }
                ebul_age[bi] = age;
            }
            st = (age < 18) ? 0 : ((age < 34) ? 1 : 2);
            if (ebul_type[bi] == EBUL_AIM) {
                k = (unsigned char)(OBJ_K_ESHOT + st);
            } else if (ebul_type[bi] == EBUL_LOW) {
                k = (unsigned char)(OBJ_K_ORB + st);
            } else {
                k = (unsigned char)(((age & 2) ? OBJ_K_EXPL_A : OBJ_K_EXPL_B) + st);
            }
            lim = (unsigned char)(FRAME_W_DOTS / 2 - (obj_w[k] << 2));
            if (ebul_hx[bi] > lim) ebul_hx[bi] = lim;
            x1 = (unsigned char)(ebul_hx[bi] + (obj_w[k] << 2));
            y1 = (unsigned char)(ebul_y[bi] + obj_h[k]);
            if (age >= 12 && pl_inv == 0 &&
                plx0 < x1 && ebul_hx[bi] < plx1 &&
                ply0 < y1 && ebul_y[bi] < ply1) {
                player_hit(hx, hy);
                ebul_age[bi] = 0;
                continue;
            }
            op[0] = (unsigned char)(FRAME_X_BYTE + (ebul_hx[bi] >> 2));
            op[1] = (unsigned char)(FRAME_Y_LINE + ebul_y[bi]);
            op[2] = (unsigned char)(k | ((ebul_hx[bi] & 3) << 6));
            op += 3;
            nobj++;
        }

        /* ---- 自機の被弾爆発 (止まった画面の中でこれだけが動く) ----
         * 芯 (A) と破片 (B) を 4 フレームおきに入れ替えて、いちばん手前の
         * 大きさで見せる (ボスの全身爆発と同じ作り)。 */
        if (frz) {
            unsigned char k = (unsigned char)(((pl_die & 4) ? OBJ_K_EXPL_A
                                                            : OBJ_K_EXPL_B) + 2);
            op[0] = (unsigned char)(FRAME_X_BYTE + (pl_ex_hx >> 2));
            op[1] = (unsigned char)(FRAME_Y_LINE + pl_ex_y);
            op[2] = (unsigned char)(k | ((pl_ex_hx & 3) << 6));
            op += 3;
            nobj++;
        }
        sub_objs_close(nobj);

        /* ---- 前のフレームの描き上がりを待って表に出し、今のフレームを
         *      投げる ----
         * main の計算 (上のぜんぶ) はサブが前のフレームを描いている間に
         * 済ませてある。ここで前の面を表に出し (= パレット差し替えだけ)、
         * 今組んだキューを投げてすぐ次の計算へ向かう。表示は 1 フレーム
         * 遅れるが、main と sub の時間が重なるぶんフレームレートが上がる
         * (実測: 直列 46.8fps → 並列 61.1fps)。 */
        sub_sync();
        palette_page((unsigned char)(1 - page));
        sub_flush_async();
        page = (unsigned char)(1 - page);
    }

}


int main(void)
{
    /* ★ crt0 の INILIB は bss をゼロクリアしない。撮影治具が「まだ
     *   タイトルにも入っていない」を見分けられるよう明示的に置く。 */
    game_state = GS_BOOT;

    /* ---- 起動シーケンス (= この順序を変えると動かなくなる) ---- */
    sub_cancel();
    {
        unsigned char p = 0x00;
        subsys_call(SCMD_CURSOR, &p, 1);    /* カーソル blink を止める */
    }
    delay_loop(500);
    subprog_init();
    palette_init();
    kb_init();
    timer_init();
    timer_start();
    input_init();
    sound_init();

    /* 爆発の絵をサブ側 VRAM の末尾 (表示されない領域) へ送る。1 回に
     * 100 byte ずつ切って WRITE 命令で運ぶ (キューは 108 byte)。 */
    {
        unsigned char si;
        const unsigned char *src = expl_data;
        for (si = 0; si < EXPL_SEGS; si++) {
            unsigned int dst = ((unsigned int)expl_seg[si * 4] << 8)
                             | expl_seg[si * 4 + 1];
            unsigned int n   = ((unsigned int)expl_seg[si * 4 + 2] << 8)
                             | expl_seg[si * 4 + 3];
            while (n) {
                unsigned char c = (n > 100) ? 100 : (unsigned char)n;
                sub_write(dst, src, c);
                sub_flush();
                dst += c;
                src += c;
                n   -= c;
            }
        }
    }

    /* タイトル → 面 → 幕、の一巡を延々と繰り返す。 */
    for (;;) {
        title_screen();
        play();
        gameover_screen();
    }

    return 0;   /* 到達しない */
}
