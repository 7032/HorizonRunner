/* ============================================================
 * c_text.c — 文字を VRAM へ直に書く (実装)
 *
 * 1 行 (8 line のうちの 1 line) ぶんを 1 つの WRITE 命令にまとめて送る。
 * WRITE は [cmd, 番地上, 番地下, 個数] の 4 byte が頭に付くので、字数を n
 * とすると 1 字 8 line で 8 * (4 + n) byte になる。キューは 108 byte
 * しか無いが、q_reserve() が溢れる前に勝手に送ってくれる。
 *
 * ★ ここは毎フレームまわる場所ではない。とはいえ掛け算の実行時呼出は
 *   避けたいので、line * 80 は「64 倍 + 16 倍」の足し算で作る (CMOC は
 *   2 の冪の掛け算だけはシフトに落とす)。
 * ============================================================ */

#include "c_subprog.h"
#include "c_text.h"
#include "../build/font.h"

/* 1 line ぶんの下書き (横 2 倍でも入るよう TEXT_MAX の 2 倍持つ) */
static unsigned char row_buf[TEXT_MAX * 2];

/* 4 bit を「1 bit を 2 bit へ」伸ばす表 (= 横 2 倍の実体) */
static const unsigned char widen[16] = {
    0x00, 0x03, 0x0C, 0x0F, 0x30, 0x33, 0x3C, 0x3F,
    0xC0, 0xC3, 0xCC, 0xCF, 0xF0, 0xF3, 0xFC, 0xFF
};

/* 文字 → 字形番号 (並びは scripts/make_font.py の ORDER と対) */
static unsigned char glyph_of(char c)
{
    if (c >= 'A' && c <= 'Z') return (unsigned char)(c - 'A' + 1);
    if (c >= '0' && c <= '9') return (unsigned char)(c - '0' + 27);
    if (c == '-') return 37;
    if (c == '.') return 38;
    if (c == '!') return 39;
    return 0;                       /* 知らない字は空白にする */
}

/* 面の先頭 + line * 80 + byte */
static unsigned int vram_at(unsigned int plane, unsigned char xb,
                            unsigned char y)
{
    unsigned int ly = (unsigned int)y;
    return plane + (ly << 6) + (ly << 4) + xb;
}


void text_put(unsigned int plane, unsigned char xb, unsigned char y,
              const char *s)
{
    const unsigned char *gp[TEXT_MAX];
    unsigned char n = 0;
    unsigned char row, i;
    unsigned int dst;

    while (s[n] && n < TEXT_MAX) {
        gp[n] = &font8[(unsigned int)glyph_of(s[n]) << 3];
        n++;
    }
    if (n == 0) return;

    dst = vram_at(plane, xb, y);
    for (row = 0; row < 8; row++) {
        for (i = 0; i < n; i++) row_buf[i] = gp[i][row];
        sub_write(dst, row_buf, n);
        dst += SCREEN_W_BYTES;
    }
}


void text_put_wide(unsigned int plane, unsigned char xb, unsigned char y,
                   const char *s)
{
    const unsigned char *gp[TEXT_MAX];
    unsigned char n = 0;
    unsigned char row, i, j;
    unsigned int dst;

    while (s[n] && n < TEXT_MAX) {
        gp[n] = &font8[(unsigned int)glyph_of(s[n]) << 3];
        n++;
    }
    if (n == 0) return;

    dst = vram_at(plane, xb, y);
    for (row = 0; row < 8; row++) {
        j = 0;
        for (i = 0; i < n; i++) {
            unsigned char v = gp[i][row];
            row_buf[j++] = widen[v >> 4];
            row_buf[j++] = widen[v & 0x0F];
        }
        sub_write(dst, row_buf, j);
        dst += SCREEN_W_BYTES;
    }
}


void text_erase(unsigned int plane, unsigned char xb, unsigned char y,
                unsigned char wb, unsigned char h)
{
    unsigned char row, i;
    unsigned int dst;

    if (wb == 0 || h == 0) return;
    for (i = 0; i < sizeof(row_buf); i++) row_buf[i] = 0;

    dst = vram_at(plane, xb, y);
    for (row = 0; row < h; row++) {
        unsigned char left = wb;
        unsigned int at = dst;
        while (left) {
            unsigned char c = (left > sizeof(row_buf))
                                  ? (unsigned char)sizeof(row_buf) : left;
            sub_write(at, row_buf, c);
            at   += c;
            left = (unsigned char)(left - c);
        }
        dst += SCREEN_W_BYTES;
    }
}
