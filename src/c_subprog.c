/* ============================================================
 * c_subprog.c — サブ CPU 描画プログラムの main 側運転手 (土台の最小版)
 *
 * 1 フレームの流れ:
 *   main 側で sub_cls() / sub_pattern() / sub_box() を積む
 *      → sub_flush() で [HALT → 共有 RAM ($FC93-) へ一括コピー →
 *         sub_call(SUB_PROG_ADDR)] を行い、サブが全部描いて戻る (= 同期)。
 *
 * 土台では同期方式に固定してある。先行プロジェクトで実績のある非同期化
 * (sub_call_async + sub_sync) の経路は asm_test.s に生きているので、
 * 速度が要るようになったら sub_flush() の CALL を差し替えればよい。
 * ============================================================ */

#include "c_subprog.h"

/* sub プログラム本体 (= bin2asm.py で asm_subprog.s から生成された rodata)。 */
extern const unsigned char subprog_bin[];
extern const unsigned int  subprog_len;

/* ----- 描画コマンドキュー (main RAM 上の影バッファ) ---------------- */

static unsigned char q_buf[SUB_QUEUE_BYTES];
static unsigned char q_len;

/* n byte ぶんの領域を確保して先頭ポインタを返す。入らなければ先に
 * flush する (= あふれても捨てない)。n は必ず SUB_QUEUE_BYTES 以下。 */
static unsigned char *q_reserve(unsigned char n)
{
    unsigned char *p;
    if ((unsigned char)(q_len + n) > SUB_QUEUE_BYTES) {
        sub_flush();
    }
    p = &q_buf[q_len];
    q_len = (unsigned char)(q_len + n);
    return p;
}

/* 積んだコマンドをまとめてサブに実行させる (完了まで待つ)。 */
void sub_flush(void)
{
    if (q_len == 0) return;

    sub_halt();
    /* 共有 RAM へのコピーはアセンブラ (sub_queue_copy)。末尾に
     * SUBCMD_END ($00) が自動で付く。 */
    sub_queue_copy(q_buf, (unsigned char *)SUB_QUEUE_BASE, q_len);
    sub_call(SUB_PROG_ADDR);
    q_len = 0;
}

/* 積んだコマンドをサブに投げ、完了を待たずに戻る (= 非同期)。
 * 前に投げたバッチがまだ動いていれば、先にその完了を待つ (sub_call_async
 * の中の sub_sync)。main はこの後すぐ次のフレームの計算に入れるので、
 * サブが描いている間 main が遊ばない (= 直列だった main と sub の時間が
 * 重なる)。描き上がりは sub_sync() で待つ。 */
void sub_flush_async(void)
{
    if (q_len == 0) return;
    sub_sync();                 /* 前のバッチ完了 (= 共有 RAM を書き換えてよい) */
    sub_halt();
    sub_queue_copy(q_buf, (unsigned char *)SUB_QUEUE_BASE, q_len);
    sub_call_async(SUB_PROG_ADDR);
    q_len = 0;
}

/* ----- 起動時の一度きり初期化 -------------------------------------- */

void subprog_init(void)
{
    /* ★ crt0 の INILIB はダミー RTS で bss をゼロクリアしないため、
     *   q_len はここで明示的に 0 にする (= 起動時の値は不定)。 */
    q_len = 0;

    /* まず転送だけ行う (exec=0 で CALL を skip)。転送中は共有 RAM が
     * chunk バッファとして使われる。 */
    sub_takeover(subprog_bin, subprog_len, SUB_PROG_ADDR, 0);

    /* 空のキュー (END だけ) を 1 回実行させて起動確認する。ここで END を
     * 書いておかないと、chunk バッファの残骸がコマンドとして解釈される
     * 恐れがある。共有 RAM への write は HALT 中のみ有効。 */
    sub_halt();
    SUB_QUEUE_BASE[0] = SUBCMD_END;
    sub_call(SUB_PROG_ADDR);
}

/* ----- 描画コマンドの積込み ---------------------------------------- */

/* 3 面を消す。**面ごとに 1 命令へ割り、1 面ずつ送り出す。**
 * 16,000 byte の消去はサブで約 190ms 掛かり、3 面を 1 つのバッチに
 * まとめると main 側の完了待ち上限 (asm_test.s の SUB_WAIT_CAP) を
 * 超える。超えると main が先に諦めて次のバッチを共有 RAM へ書き、
 * 消去中のサブがそれを踏む (= 消去の直後に描いた絵が虫食いになる)。 */
void sub_cls(void)
{
    unsigned char pl;
    for (pl = 0; pl < 3; pl++) {
        unsigned char *p = q_reserve(2);
        p[0] = SUBCMD_CLS;
        p[1] = pl;
        sub_flush();
    }
}

void sub_scene(unsigned char clr_from, unsigned char clr_lines,
               unsigned char hz, const unsigned char *bands,
               unsigned char nbands)
{
    unsigned char i;
    unsigned char n2 = (unsigned char)(nbands * 2);
    unsigned char *p = q_reserve((unsigned char)(5 + n2));
    p[0] = SUBCMD_SCENE;
    p[1] = clr_from;
    p[2] = clr_lines;
    p[3] = hz;
    p[4] = nbands;
    for (i = 0; i < n2; i++) {
        p[5 + i] = bands[i];
    }
}

void sub_sprite(unsigned char x, unsigned char y, unsigned char ph)
{
    unsigned char *p = q_reserve(4);
    p[0] = SUBCMD_SPRITE;
    p[1] = x;
    p[2] = y;
    p[3] = ph;
}

void sub_page(unsigned char page)
{
    unsigned char *p = q_reserve(2);
    p[0] = SUBCMD_PAGE;
    p[1] = page;
}

/* OBJS 命令を開く。個数の枠 (2 + 3*max byte) を先に確保し、呼び手が
 * 返されたポインタへ [x, y, k|ph<<6] を直接書く (= 二度写しを避ける)。
 * 閉じるまで他の積込みをしてはならない (閉じる時に未使用分を返すため)。 */
static unsigned char *objs_hdr;

unsigned char *sub_objs_open(unsigned char max)
{
    objs_hdr = q_reserve((unsigned char)(2 + max + max + max));
    objs_hdr[0] = SUBCMD_OBJS;
    return objs_hdr + 2;
}

void sub_objs_close(unsigned char n)
{
    unsigned char *end = objs_hdr + 2 + n + n + n;
    if (n == 0) {
        q_len = (unsigned char)(objs_hdr - q_buf);       /* 命令ごと取り消す */
    } else {
        objs_hdr[1] = n;
        q_len = (unsigned char)(end - q_buf);
    }
}

void sub_write(unsigned int dst, const unsigned char *src, unsigned char n)
{
    unsigned char i;
    unsigned char *p = q_reserve((unsigned char)(4 + n));
    p[0] = SUBCMD_WRITE;
    p[1] = (unsigned char)(dst >> 8);
    p[2] = (unsigned char)dst;
    p[3] = n;
    for (i = 0; i < n; i++) {
        p[4 + i] = src[i];
    }
}
