/* ============================================================
 * c_sound.h — PSG 8 ビート BGM と効果音
 * ============================================================ */
#ifndef C_SOUND_H
#define C_SOUND_H

void sound_init(void);
void sound_tick(void);
void sfx_shot(void);
void sfx_destroy(void);

/* 検証治具がリンクマップから読み取る現在の 8 分音符位置。 */
extern unsigned char sound_step;

#endif
