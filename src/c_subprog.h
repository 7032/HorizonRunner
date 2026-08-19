/* ============================================================
 * c_subprog.h — サブ CPU 独自描画プログラム (土台の最小版) の C API
 *
 * 構成要素:
 *   - 低レベル:   sub_halt / sub_release / sub_call / sub_takeover
 *                 (= asm_test.s 実装、sub への code/data 転送と JSR)
 *   - 高レベル:   subprog_init / sub_cls / sub_pattern / sub_box /
 *                 sub_flush (= c_subprog.c 実装、描画コマンドキュー)
 *   - キー入力:   kb_init / key_check / break_check (= asm_kbd.s 実装)
 *   - タイマ:     timer_init / timer_start / timer_get / timer_consume
 *                 (= asm_timer.s 実装)
 *
 * 描画は B プレーン 1 枚だけのモノクロ 2 値。色引数は存在しない。
 * ============================================================ */

#ifndef C_SUBPROG_H
#define C_SUBPROG_H

/* ---- sub 上のロード位置 ----
 *
 * ★ 配置アドレスは config.mk / asm_subprog.s の equ / ここ の 3 箇所に
 *   書かれている。片方だけ直すと転送先と参照先が静かにずれ、エラーも
 *   例外も出ずに絵が化ける。make のたびに scripts/check_layout.py が
 *   3 者を突き合わせて検査する。**正は config.mk である。**
 *   コード中にアドレスをハードコードしないこと。 */

/* サブ側 作業変数 (32 byte 枠) */
#define SUB_WORK_ADDR     0xC000
/* 面ごとの消し込み表 (2 面 x 26 件 x 4 byte = 208 byte) */
#define SUB_TABLE_ADDR    0xC020
/* subprog コード本体の入口。$C100-$CF7F (= 3,712 byte まで)。
 * 絵 (自機 / 敵 / 地上物 / 岩) と床の模様表もここに続けて置く。
 * make 後に build/subprog.bin のサイズを check_layout.py が検査し、
 * SUB_PROG_ADDR + size が SUB_CODE_END を超えたらビルドを止める
 * (= 超えるとサブ CPU のスタックを踏んで暴走する)。 */
#define SUB_PROG_ADDR     0xC100
#define SUB_CODE_END      0xCF80
/* スプライト原本の置場 (R プレーン。表示に使わない RAM) */
#define SUB_SPRITE_ADDR   0x4000

/* ---- 描画コマンドキュー (= 共有 RAM 上) ----
 *
 * 共有 RAM は main $FC80-$FCFF / sub $D380-$D3FF の 128 byte。
 * うち $FC80-$FC92 は sub_call / sub_takeover が TEST cmd 列で使うので、
 * キューはその後ろの $FC93 から張る。$FC93-$FCFF = 109 byte あり、
 * 末尾 1 byte を終端 ($00) に使うので実効ペイロードは 108 byte。
 *
 * 積込みは main RAM 上の影バッファに対して行い、sub_flush() の中で
 * 「HALT → 共有 RAM へ一括コピー → sub_call」とする。 */
#define SUB_QUEUE_BASE    ((volatile unsigned char *)0xFC93)
#define SUB_QUEUE_BYTES   108

/* sub 描画コマンドコード (= asm_subprog.s のディスパッチと要一致) */
#define SUBCMD_END        0x00   /* (なし)          キュー終端             */
#define SUBCMD_CLS        0x01   /* p               1 面を全消去 (0 B/1 R/2 G) */
#define SUBCMD_SCENE      0x04   /* ct,cn,hz,n,...  空消去+地平線+床の帯   */
#define SUBCMD_SPRITE     0x05   /* x,y,ph          自機を貼る             */
#define SUBCMD_PAGE       0x07   /* p               描画先の面を選ぶ       */
#define SUBCMD_OBJS       0x09   /* n,[x,y,k|ph<<6] 敵/地上物/岩/自弾/爆発を n 個 */
#define SUBCMD_WRITE      0x0A   /* ah,al,n,[byte]  サブ側の番地へ n byte 書く */
/* ($06 RECT / $08 ENEMY は OBJS に置き換わり撤去した) */

/* 画面寸法 */
#define SCREEN_W_BYTES    80     /* 横 80 byte (= 640 px) */
#define SCREEN_H_LINES    200    /* 縦 200 line */

/* ---- 描画枠 (= 384x96 dot) ----
 *
 * FM-7 の 640x200 は画素が正方形ではなく縦長で、4:3 の画面に映すと
 * 1 画素の縦横比はおよそ 1 : 2.4 になる。よって見かけの縦横比は
 * 「横ドット数 : 縦ドット数 x 2.4」で考えねばならない。
 *   256x128 → 見かけ 256 : 307 = 縦長
 *   384x96  → 見かけ 384 : 230 = 横長 (約 1.67:1) ← 採用
 * 枠は画面中央に置く。左上の B プレーンアドレス = 52*80 + 16 = $1050。
 * 行送りは +80。枠幅 48 byte は 6 の倍数なので、サブ側の PSHU 塗り
 * (D/X/Y の 6 byte を 1 命令で書く) がちょうど 8 回で 1 行を埋める。 */
#define FRAME_X_BYTE      16     /* 枠左上の x (byte 単位) */
#define FRAME_Y_LINE      52     /* 枠左上の y (line 単位) */
#define FRAME_W_BYTES     48     /* 横 48 byte = 384 dot */
#define FRAME_H_LINES     96     /* 縦 96 line */
#define FRAME_W_DOTS      384

/* ---- 自機と敵 (= scripts/make_sprite.py が焼く) ---- */
#define SPR_W_BYTES       4      /* 貼り付け幅 (byte)。24 dot + 位相ずらし */
#define SPR_H_LINES       20
#define SPR_W_DOTS        24
/* 敵の絵の高さ (line)。幅は 2/3/4 byte。生成器 scripts/make_sprite.py の
 * ENEMY_ART と必ず一致させること (= 枠からはみ出す判定に使う)。 */
#define ENM_H0            6
#define ENM_H1            10
#define ENM_H2            14

/* ----- 低レベル (asm_test.s 実装) ----- */
void          sub_wait_ready(void);
void          sub_wait_busy(void);
void          sub_halt(void);
void          sub_release(void);
/* TEST CALL を発行してサブの完了を待ってから戻る (= 同期) */
void          sub_call(unsigned int addr);
/* TEST CALL を発行して待たずに戻る (= 非同期)。土台では未使用だが、
 * 後で並列化する時のために経路は生かしてある。 */
void          sub_call_async(unsigned int addr);
/* 非同期で投げたバッチの完了待ち (未完了バッチが無ければ no-op) */
void          sub_sync(void);
/* src から dst へ n byte コピーし、dst[n] に $00 (= SUBCMD_END) を置く */
void          sub_queue_copy(const void *src, unsigned char *dst,
                             unsigned int n);
/* ROM の TEST MOVE 経由の転送 + (exec != 0 なら) 実行。
 * ★ 転送先に VRAM (= R/G プレーン) は指定できない。通常 RAM 専用。 */
void          sub_takeover(const void *code, unsigned int len,
                           unsigned int dst, unsigned int exec);

/* ----- 起動時の一度きり初期化 (c_subprog.c 実装) ----- */

/* sub の SUB_PROG_ADDR に subprog 本体を転送し、空キューで 1 回起動して
 * 疎通を確認する。描画 API を使う前に必ず 1 回呼ぶこと。 */
void          subprog_init(void);

/* ----- 描画コマンドの積込み (c_subprog.c 実装) ----- */

void          sub_cls(void);   /* 3 面とも全消去 (面ごとに 1 回ずつ送る) */

/* 空の消去 + 地平線 + 床の帯を 1 コマンドで積む。
 *   clr_from/clr_lines : 空に戻す枠内 line の範囲 (clr_lines=0 で消さない)
 *   hz                 : 地平線の枠内 line。ここに白い横線が引かれる
 *   bands              : [行数, 塗る byte 値] の並び。地平線の 1 行下から
 *                        順に塗る。枠からはみ出す分はサブ側で切り捨てる
 *   nbands             : 帯の数 */
void          sub_scene(unsigned char clr_from, unsigned char clr_lines,
                        unsigned char hz, const unsigned char *bands,
                        unsigned char nbands);
/* 自機を貼る。x は byte 単位 (画面全体で 0-79)、y は line (0-199)、
 * ph は 0-7 dot の横位相。 */
void          sub_sprite(unsigned char x, unsigned char y, unsigned char ph);
/* 以後の描画先の面を選ぶ (= ダブルバッファリング)。0 = B plane / 1 = R plane。
 * 表示面の切替は palette_page() が行う。 */
void          sub_page(unsigned char page);
/* 敵 / 地上物 / 岩 / 自弾 / 爆発を n 個まとめて 1 命令で積む。x は byte 単位
 * (画面全体で 0-79)、y は line (0-199)、k は絵の種類 (build/sprite_geo.h)、
 * ph は 0-3 (2 dot 単位の横位相)。k は 0-63。**キューは 108 byte しか無く、あふれる
 * とサブを 2 回呼ぶことになり往復だけで約 2ms 失う** ので、毎フレームの絵
 * (自弾・敵・地上物・岩) は全部これに詰めて 1 命令で送る。1 個 3 byte。
 *   p = sub_objs_open(max);   最大 max 個ぶんの枠を確保し、書込先を返す
 *   p[0]=x; p[1]=y; p[2]=k|(ph<<6); p+=3;  … を n 回
 *   sub_objs_close(n);        個数を確定し、余った枠を返す
 * open から close の間に他の sub_* を呼んではならない。 */
unsigned char *sub_objs_open(unsigned char max);
void          sub_objs_close(unsigned char n);
/* サブ側の番地 dst へ src から n byte 書く (n は 105 以下)。書くのはサブ CPU
 * である (メイン CPU から VRAM は見えない)。起動時の絵の転送と文字描画
 * (c_text.c) が使う。毎フレームは使わないこと。ROM の TEST MOVE は VRAM を
 * 転送先に取れないので、描画プログラムのコマンドとして運ぶ。 */
void          sub_write(unsigned int dst, const unsigned char *src,
                       unsigned char n);
/* 積んだコマンドをまとめてサブに実行させる (完了まで待つ)。 */
void          sub_flush(void);
/* 同上だが完了を待たない。完了は sub_sync() で待つ。毎フレームの描画は
 * こちらを使い、main の計算とサブの描画を重ねる。 */
void          sub_flush_async(void);

/* ----- キー入力 (asm_kbd.s 実装) ----- */
void          kb_init(void);
unsigned char key_check(void);
unsigned char break_check(void);
void          palette_init(void);
/* 表示する面を選ぶ (= ダブルバッファリングの表裏切替)。0 = B plane / 1 = R plane。
 * パレットを差し替えるだけなので VRAM は 1 byte も動かない。 */
void          palette_page(unsigned char page);

/* ----- 周期タイマ (asm_timer.s 実装。1 tick = 約 2ms) ----- */
void          timer_init(void);
void          timer_start(void);
unsigned int  timer_get(void);
void          timer_consume(unsigned int n);
/* PSG のレジスタ選択と IRQ 側の入力処理が重ならないよう、短い I/O 列を
 * 囲む。irq_restore は保存した CC の状態へ戻す。 */
unsigned char irq_save(void);
void          irq_restore(unsigned char cc);

#endif
