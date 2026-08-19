/* ============================================================
 * c_input.h — ゲーム用 入力モジュール の C インタフェース
 *
 * 実体は src/asm_input.s (IRQ 駆動)。
 *
 * 【操作仕様】
 *   テンキー 8 / 2 / 4 / 6 … 上 / 下 / 左 / 右 へ移動
 *   テンキー 5             … 停止
 *   テンキー 7 / 9 / 1 / 3 … 斜め (左上 / 右上 / 左下 / 右下) の直接指定
 *   カーソルキー           … 8/2/4/6 の代替
 *   BREAK キー             … ショット (押しっぱなしで自動連射)
 *
 * 【移動は状態保持型】
 *   この機種のキーボードは 「キーを離した」 通知を出さない (BREAK キーを除く)。
 *   そのため方向は 「押した瞬間に確定し、 離しても保持される」。 止めるには
 *   テンキー 5 を押す (= input_dir_clear() と同じ)。
 *   斜めは縦横それぞれの軸を独立に持つので、 8 の後に 6 を押せば右上になる。
 *
 * 【使い方】
 *   起動時 (kb_init(); timer_init(); の後に 1 回):
 *       input_init();
 *
 *   毎フレーム:
 *       unsigned char d = input_dir();
 *       if (d & IN_UP)    py--;
 *       if (d & IN_DOWN)  py++;
 *       if (d & IN_LEFT)  px--;
 *       if (d & IN_RIGHT) px++;
 *
 *       n = break_shots();            // 前フレーム以降のショット数
 *       while (n--) fire_bullet();    // 弾スロットが尽きたら break; でよい
 *
 *       e = input_edges();            // 押された瞬間だけ取りたいもの
 *       if (e & IN_PAUSE) paused = !paused;
 * ============================================================ */

#ifndef C_INPUT_H
#define C_INPUT_H

/* --- 入力ビット定義 (asm_input.s の IN_* と必ず一致させること) --- */
#define IN_UP       0x01    /* 上    (テンキー 8 / カーソル上) */
#define IN_DOWN     0x02    /* 下    (テンキー 2 / カーソル下) */
#define IN_LEFT     0x04    /* 左    (テンキー 4 / カーソル左) */
#define IN_RIGHT    0x08    /* 右    (テンキー 6 / カーソル右) */
#define IN_STOP     0x10    /* 停止  (テンキー 5)              */
#define IN_FIRE     0x20    /* 発射  (SPACE。 主操作は BREAK)  */
#define IN_PAUSE    0x40    /* ポーズ ('p' / 'P')              */
#define IN_START    0x80    /* 開始/決定 (RETURN)              */

#define IN_DIRMASK  0x0F    /* 方向 4 ビットのマスク */

/* 起動時に 1 回。 timer_init() の後に呼ぶこと (内部状態と BREAK 初期レベルを揃える)。 */
void          input_init(void);

/* 保持中の移動方向ビット (IN_UP / IN_DOWN / IN_LEFT / IN_RIGHT の OR)。
 * 斜めのときは 2 ビット立つ。 停止中は 0。 */
unsigned char input_dir(void);

/* 移動方向を停止状態に戻す (テンキー 5 と同じ効果)。 自機ミス時などに使う。 */
void          input_dir_clear(void);

/* 生の 「直近に押された」 ビットマップ。
 * ※ この機種は離鍵を通知しないので 「押しっぱなし」 ではなく
 *   「ごく最近 (約 130ms 以内) 押された」 の意味。 移動判定には使わず、
 *   input_dir() を使うこと。 */
unsigned char input_state(void);

/* 前回呼出以降に押されたキーのビット和。 読むとクリアされる (押下エッジ)。
 * メニュー操作やポーズのトグルはこちらを使うと取りこぼさない。 */
unsigned char input_edges(void);

/* 前回呼出以降に発生したショット数 (人間の連打 + オート連射の合成)。
 * 読むとクリアされる。 255 で飽和する。 */
unsigned char break_shots(void);

/* BREAK 押下中なら 1、 でなければ 0 (チャタリング除去済の確定状態)。
 * ※ c_subprog.h の break_check() は $FD04 を直接読む生ポーリングで、
 *   こちらは IRQ 側で確定させた状態。 用途に応じて使い分ける。 */
unsigned char break_held(void);


/* ============================================================
 * ジョイスティック (FM 音源ポート経由。 本作ではスタブ。 実体は src/asm_input.s 末尾)
 *
 * 【刺さっていない環境が既定である】
 *   対象機種の初代 FM-7 は本体にジョイスティックの口を持たない。 口は増設の
 *   FM 音源カード側にあるので、 **無い方が普通**である。 有無は起動時に
 *   1 回だけ判別し (joy_init)、 無ければ以後 _joy_bits_var は 0 (= 中立) の
 *   まま動かない。 判別にも読み取りにもループが無いため、 刺さっていない
 *   環境で待ち続ける経路は存在しない。
 *
 * 【ゲーム側は基本的に何もしなくてよい】
 *   方向は input_dir() が、 発射は break_shots() が、 それぞれ内部で
 *   ジョイスティックを取り込み済みである。 下の関数を直接使うのは
 *   タイトル画面の 「何か押されたか」 のような特別な場面だけでよい。
 * ============================================================ */

/* --- joy_bits() のビット (方向 4 つは IN_* と並びが同じ) --- */
#define JOY_UP      0x01
#define JOY_DOWN    0x02
#define JOY_LEFT    0x04
#define JOY_RIGHT   0x08
#define JOY_TRIG1   0x10    /* トリガ 1 */
#define JOY_TRIG2   0x20    /* トリガ 2 */

/* 起動時に 1 回。 input_init() が内部で呼ぶので、 ゲーム側から呼ぶ必要は無い。
 * 戻り値 1 = ジョイスティックが読める / 0 = 音源カードが無い。 */
unsigned char joy_init(void);

/* 判別結果 (キャッシュ済み。 I/O は触らない)。 1 = 読める / 0 = 無い。 */
unsigned char joy_present(void);

/* スティックとトリガの状態 (JOY_* の OR)。 **0 = 何も倒れていない**。
 * ジョイスティックは離した事が判るので、 これは 「今の状態」 そのものである
 * (キー入力の input_state() のような 「直近に押された」 ではない)。 */
unsigned char joy_bits(void);

/* トリガ 1 / 2 のどちらかが押されていれば 1。 */
unsigned char joy_trig(void);

#endif /* C_INPUT_H */
