/* ============================================================
 * c_sound.c — AY-3-8910 PSG だけで鳴らす 8 ビート
 *
 *   A: シンセベース。常に残し、曲の重心にする。
 *   B: シンセドラム。キックは矩形波の降下、スネアとハイハットは PSG
 *      ノイズの短い減衰で作る。
 *   C: 薄い和音。発射音と破壊音の間だけ効果音へ貸す。
 *
 * 1 step は 8 分音符、8 step で 1 小節。実行時の音程計算は行わず、
 * period と音量の表を引くだけにする。
 * ============================================================ */

#include "c_sound.h"
#include "c_subprog.h"

#define PSG_CTRL (*(volatile unsigned char *)0xFD0D)
#define PSG_BUS  (*(volatile unsigned char *)0xFD0E)

/* R7。A/C は tone、B は drum_mode() が tone/noise を切り替える。
 * bit7 は出力のまま保つ。 */
#define MIX_KICK   0xB8
#define MIX_NOISE  0xAA

#define STEP_FRAMES 8        /* 8 分音符。約 230 BPM */
#define SONG_LEN    32       /* 4 小節 */

#define REST 0
#define C2 1175
#define D2 1047
#define E2  932
#define F2  880
#define G2  784
#define A2  699
#define C3  587
#define D3  523
#define E3  466
#define F3  440
#define G3  392
#define A3  349
#define C4  294
#define E4  233
#define G4  196
#define A4  175
#define C5  147
#define E5  117
#define A5   87
#define C6   73

/* イ短調。根音と 5 度を交互に刻むシンセベースが主役。 */
static const unsigned int bass[SONG_LEN] = {
    A2,A3,A2,E3, A2,A3,G2,E3,
    F2,F3,F2,C3, G2,G3,G2,D3,
    A2,A3,A2,E3, F2,F3,F2,C3,
    D2,D3,F2,A2, E2,E3,G2,E3
};

/* C 声は長いパルスだけ。音数を抑えてドラムとベースを前へ出す。 */
static const unsigned int pad[SONG_LEN] = {
    A4,REST,E4,REST, A4,REST,E4,REST,
    A4,REST,F3,REST, G4,REST,D3,REST,
    A4,REST,E4,REST, A4,REST,F3,REST,
    A4,REST,F3,REST, G4,REST,E4,REST
};

/* 0=キック、1=ハイハット、2=スネア、3=ハイハット。これを 2 回で 1 小節。 */
static const unsigned char drum[8] = { 0,1,2,1, 0,1,2,1 };

/* 残りフレームを添字として後ろから読む。 */
static const unsigned int kick_p[4] = { 980,720,470,280 };
static const unsigned char kick_v[4] = { 6,9,12,15 };
static const unsigned char snare_v[4] = { 0,5,10,14 };
static const unsigned char hat_v[2] = { 0,7 };

static const unsigned int shot_p[3] = { E5,A5,C6 };
static const unsigned int boom_p[6] = { C5,A4,G4,E4,C4,980 };

unsigned char sound_step;
static unsigned char step_timer;
static unsigned char drum_kind;
static unsigned char drum_left;
static unsigned char se_kind;       /* 0 無し / 1 発射 / 2 破壊 */
static unsigned char se_step;
static unsigned char se_timer;

static void psg_write(unsigned char reg, unsigned char val)
{
    unsigned char cc = irq_save();
    PSG_BUS = reg;  PSG_CTRL = 3; PSG_CTRL = 0;
    PSG_BUS = val;  PSG_CTRL = 2; PSG_CTRL = 0;
    irq_restore(cc);
}

static void tone(unsigned char ch, unsigned int p, unsigned char vol)
{
    unsigned char r = (unsigned char)(ch + ch);
    if (!p) { psg_write((unsigned char)(8 + ch), 0); return; }
    psg_write(r, (unsigned char)p);
    psg_write((unsigned char)(r + 1), (unsigned char)((p >> 8) & 15));
    psg_write((unsigned char)(8 + ch), vol);
}

static void drum_start(unsigned char k)
{
    drum_kind = k;
    if (k == 0) {
        drum_left = 4;
        psg_write(7, MIX_KICK);
    } else if (k == 2) {
        drum_left = 4;
        psg_write(6, 9);
        psg_write(7, MIX_NOISE);
    } else {
        drum_left = 2;
        psg_write(6, 2);
        psg_write(7, MIX_NOISE);
    }
}

static void drum_tick(void)
{
    if (!drum_left) return;
    drum_left--;
    if (drum_kind == 0) {
        tone(1, kick_p[drum_left], kick_v[drum_left]);
    } else if (drum_kind == 2) {
        psg_write(9, snare_v[drum_left]);
    } else {
        psg_write(9, hat_v[drum_left]);
    }
}

static void music_apply(void)
{
    tone(0, bass[sound_step], 12);
    if (!se_kind) tone(2, pad[sound_step], 5);
    drum_start(drum[sound_step & 7]);
}

void sound_init(void)
{
    psg_write(8, 0); psg_write(9, 0); psg_write(10, 0);
    psg_write(7, MIX_KICK);
    sound_step = SONG_LEN - 1;
    step_timer = 0;
    drum_kind = 0; drum_left = 0;
    se_kind = 0; se_step = 0; se_timer = 0;
}

void sound_tick(void)
{
    if (!step_timer) {
        step_timer = STEP_FRAMES;
        sound_step++;
        if (sound_step >= SONG_LEN) sound_step = 0;
        music_apply();
    }
    step_timer--;
    drum_tick();

    if (se_kind) {
        if (se_timer) {
            se_timer--;
        } else {
            se_step++;
            if ((se_kind == 1 && se_step >= 3) ||
                (se_kind == 2 && se_step >= 6)) {
                se_kind = 0;
                tone(2, pad[sound_step], 5);
            } else {
                se_timer = (se_kind == 1) ? 0 : 1;
                tone(2, (se_kind == 1) ? shot_p[se_step] : boom_p[se_step],
                     (se_kind == 1) ? 11 : 15);
            }
        }
    }
}

void sfx_shot(void)
{
    if (se_kind == 2) return;
    se_kind = 1; se_step = 0; se_timer = 0;
    tone(2, shot_p[0], 11);
}

void sfx_destroy(void)
{
    se_kind = 2; se_step = 0; se_timer = 1;
    tone(2, boom_p[0], 15);
}
