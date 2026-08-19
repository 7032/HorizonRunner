/* ============================================================
 * c_text.h — 文字を VRAM へ直に書く (タイトル / 得点 / 残機 / 幕)
 *
 * サブ CPU は自前の描画プログラムに乗っ取られているので、サブシステム
 * ROM の PRINT はもう使えない。サブ側のコード枠も残り僅かでフォントを
 * 置けない。そこで **字形はメイン側に持ち**、既存の WRITE 命令に載せて送り、
 * サブ CPU に VRAM へ書かせる (サブ側は 1 byte も増えない)。
 *
 * 使いどころ:
 *   - 毎フレームは使わない。1 回の呼出しで最大 8 個の WRITE 命令を積む
 *     ので、キューの都合でサブを何度か呼ぶことになる。
 *   - 値が変わったフレームだけ書き直すこと (得点・残機)。
 *   - 呼んだ後は sub_flush() で送り出す (積むだけでは出ない)。
 *   - 非同期の描画が走っている最中に呼んではならない。先に sub_sync()。
 *
 * 置き場所は「面の先頭 + line * 80 + byte」。line と byte は画面全体
 * (0-199 / 0-79) で数える。
 * ============================================================ */

#ifndef C_TEXT_H
#define C_TEXT_H

/* 書き込む面 (= c_subprog.h の三枚と同じ) */
#define TEXT_PLANE_0   0x0000   /* ダブルバッファリングの面 0 (白) */
#define TEXT_PLANE_1   0x4000   /* ダブルバッファリングの面 1 (白) */
#define TEXT_PLANE_G   0x8000   /* カラープレーン (緑) */

/* 1 回に書ける字数 */
#define TEXT_MAX       32

/* 8x8 のまま書く。x は byte (= 8 dot) 単位なので字は byte 境界に乗る。 */
void text_put(unsigned int plane, unsigned char xb, unsigned char y,
              const char *s);

/* 横だけ 2 倍に伸ばして書く (1 字 16 dot)。FM-7 の画素は縦長なので、
 * これでようやく字がほぼ正方に見える。題字や幕はこちら。 */
void text_put_wide(unsigned int plane, unsigned char xb, unsigned char y,
                   const char *s);

/* 矩形を黒で潰す (幅は byte 単位)。書いた字を消すのに使う。 */
void text_erase(unsigned int plane, unsigned char xb, unsigned char y,
                unsigned char wb, unsigned char h);

#endif
