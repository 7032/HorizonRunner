* ============================================================
* asm_test.s — main から「TEST コマンド」 経由でサブ
*                  CPU にプログラム転送 + 実行する API
*
* TEST cmd は サブシステム ROM の 拡張コマンド領域の CMD $3F
* にある TEST/DEBUG 機能。 共有 RAM にキーワード列とサブコマンド
* ($91 MOVE / $93 CALL / $90 END) を並べて発火すると、 sub の
* workspace 上で memory copy や任意番地 JSR を実行してくれる。
*
* 本テンプレでは:
*   - 自前の sub プログラム (= asm_subprog.s) を sub の $C300 に転送
*   - sprite データを sub の $C500 に転送
*   - sub_call($C300) で sub プログラムを 1 回起動 (= 共有 RAM の cmd
*     を見て 1 つ処理して RTS)
* という用途で使う。
*
* 共有 RAM レイアウト (main 側):
*   $FC80    : $00          ; 未使用
*   $FC81    : $00          ; 未使用
*   $FC82    : $3F          ; TEST コマンド
*   $FC83-$FC8A: 8B 0       ; キーワード (FM-7 では照合しないが配置必須)
*   $FC8B    : サブコマンド ; $91=MOVE / $93=CALL / $90=END
*   $FC8C+   : サブコマンド パラメータ
*     MOVE   : src(2) + dst(2) + len(2) + END
*     CALL   : addr(2) + END
*
* HALT プロトコル:
*   1. HALT 要求 ($FD05=$80)
*   2. wait BUSY ($FD05 bit7=1) ← 省略不可 (省くと write 反映されない)
*   3. 共有 RAM に TEST cmd 列を書込
*   4. RELEASE ($FD05=$00) で sub 実行開始
*
* ============================================================

IO_SUBCTRL      equ     $FD05
SHARED_RAM      equ     $FC80
SHARED_SUB      equ     $D3A0           * sub 側から見た $FCA0 (= 共有 RAM 後半)

* ---- sub_wait_ready の空転上限 -------------------------------------
* BUSY ($FD05 bit7) が落ちるのを待つループの上限。 超えたら諦めて戻る。
* ここを無限ループにすると 「黒画面で固まる」 という最悪の壊れ方をする
* (= 画面が出ないので原因の切り分けも効かない) ので必ず上限を付ける。
*
* ループ 1 周 = lda ext(5) + bpl(3) + leax -1,x(5) + bne(3) = 16 cycle。
* main は 1.2MHz (= 1 cycle 0.833us)。
*
* ★ 2026-08-11 改定: 5000 周 (= 80,000 cycle = 約 66.6ms) では
*   **CLS 1 発を待ち切れない**。 CLS は 60,000 sub cycle あり、 サブは
*   VRAM ゲート ($D409) を開けている間 全命令一律 32% 速度になるので、
*   実時間では 60,000 / (2.016MHz x 0.32) = 約 93ms かかる。 上限に
*   引っ掛かって main が先に諦めると、 次フレームの sub_flush() が
*   **CLS の途中でサブを HALT してコマンドキューを上書きする**。
*   そのバッチの CLS より後ろ (= 自機 BLIT / STARS / HUD の TEXT) が
*   丸ごと失われ、 さらにサブは古い U のままキューを読み直すので
*   「TEXT のパラメータ列を FILL コマンドとして実行する」 等の
*   派手な画面破壊が起きる (= 面切替時の帯状ゴミの正体)。
*
*   40,000 周 = 640,000 cycle = 約 533ms。 いちばん長い 1 命令は 1 面
*   ぶんの CLS (16,000 byte = 約 190ms) で、 その約 2.8 倍を取ってある。
*   ★ CLS は面ごとに 1 命令へ割ってある (asm_subprog.s の cmd_cls)。
*     3 面を 1 命令で消すと 570ms 掛かってこの上限を超え、 上記の
*     「消去中のサブをキュー上書きで踏む」 事故がそのまま起きる。
*   本当にサブが死んだ場合も 「そのフレームだけ 533ms 遅い」 に劣化する
*   だけでハングしない。 (c_main.c の PACE_SAFETY_CAP と同じ流儀。)
SUB_WAIT_CAP    equ     40000

* ---- sub_wait_busy の空転上限 (= BUSY 立ち上がり待ち) ---------------
* RELEASE してからサブシステム ROM が 「コマンドを受け取った」 印として
* $D40A に書き込む (= BUSY を立てる) までには遅れがある。 実測で
* **36 - 330 main cycle**、 中央値 36 cycle。 一方 main が RELEASE 後
* 最初に $FD05 を読むのは約 41 cycle 後なので、 **半分近くの呼び出しで
* 「まだ BUSY が立っていない」 状態を読んでしまう**。 そのまま
* sub_wait_ready に入ると bit7=0 が即座に成立し、 サブが 1 命令も
* 実行しないうちに 「完了した」 として戻ってしまう。
*
* 通常のバッチ (約 8,000 sub cycle = 4ms) はフレームが明けるまでに
* 勝手に終わるので実害が出ないが、 CLS (30ms 超) だけは次フレームまで
* 食い込み、 上記の 「キュー上書き」 事故を起こす。
*
* そこで **完了待ちの前に立ち上がりを待つ**。 上限 400 周
* (= 6,400 cycle = 約 5.3ms) は実測最大 330 cycle の約 19 倍。 万一
* サブがコマンドを拾い損ねても、 ここで諦めて完了待ちへ抜けるだけで
* ハングしない。
SUB_BUSY_CAP    equ     400

* ---- 保険待ち (= BUSY 落ち直後に TEST を書かないための猶予) --------
* $FD05 bit7 が落ちてから サブシステム ROM がコマンドの後始末を終えて
* コマンド待ちループに戻るまでには、 サブ側で数十命令ぶんの隙がある。
* この隙に新しい TEST コマンド列を書いて RELEASE すると、 そのバッチが
* 丸ごと拾われずに捨てられる (= 症状は 「1 フレーム描画が抜ける」 だけで、
* 暴走しないので極めて気付きにくい)。
*
* ループ 1 周 = decb(2) + bne(3) = 5 cycle。 60 周 = 300 cycle
* = 300 x 0.833us = 250us = 0.25ms。 サブ換算で約 160 sub cycle あり、
* ROM ハンドラの後始末 (数十命令) を跨ぐのに十分。
SUB_POST_WAIT   equ     60

                section code

                export  _sub_wait_ready
                export  _sub_wait_busy
                export  _sub_halt
                export  _sub_release
                export  _sub_call
                export  _sub_call_async
                export  _sub_sync
                export  _sub_queue_copy
                export  _sub_takeover


* void sub_wait_ready(void) — $FD05 bit7=0 まで待つ (上限付き)
*
* $FD05 = (_subHalted || _subBusy) ? $FE : $7E。
*
* ★ 2026-08-11 実測で確定: BUSY はサブシステム ROM 自身が上げ下げして
*   おり、 **立ち下がりは「コマンド処理完了」 を正しく意味する**。 自前の
*   subprog は ROM の TEST CALL ハンドラの内側で走るので、 subprog の
*   実行区間はまるごと BUSY=1 の区間に含まれる。
*
* ★ ただし **単独で使ってはならない**。 RELEASE から ROM が BUSY を
*   立てるまでに 36-330 main cycle の遅れがあり、 main が最初に $FD05 を
*   読むのは約 41 cycle 後なので、 **半分近くの呼び出しで「まだ立って
*   いない BUSY」 を読んで即抜けする**。 必ず _sub_wait_busy と対にする
*   こと (= _sub_call がそうしている)。
*
* X を破壊しない (= 呼び出し側の都合を変えないため pshs/puls で退避)。
_sub_wait_ready:
                pshs    x
                ldx     #SUB_WAIT_CAP
.wr:
                lda     IO_SUBCTRL
                bpl     .wr_done        * bit7=0 → ready
                leax    -1,x
                bne     .wr
                                        * 上限到達 = 諦めて戻る (ハングしない)
.wr_done:
                puls    x,pc


* void sub_wait_busy(void) — $FD05 bit7=1 (= サブがコマンドを拾った) まで待つ
*
* **RELEASE の直後にだけ使う。** 「サブがまだ動き出していないのに
* sub_wait_ready が即抜けする」 事故 (= SUB_BUSY_CAP のコメント参照) を
* 塞ぐためのもので、 これと sub_wait_ready を必ずこの順で対にする。
*
* 非同期パス (sub_sync) では使わない。 あちらは 1 フレーム経ってから
* 呼ばれるので BUSY はとっくに落ちており、 立ち上がり待ちは上限まで
* 空転するだけで害しかない。
*
* X を破壊しない。
_sub_wait_busy:
                pshs    x
                ldx     #SUB_BUSY_CAP
.wb:
                lda     IO_SUBCTRL
                bmi     .wb_done        * bit7=1 → サブが受け取った
                leax    -1,x
                bne     .wb
                                        * 上限到達 = 諦めて戻る (ハングしない)
.wb_done:
                puls    x,pc


* void sub_sync(void) — 非同期で投げたバッチの完了待ち (+ 保険待ち)
*
* _sub_pending が 0 のとき (= 同期モード、 または投げっぱなしのバッチが
* 無いとき) は何もせずに戻る (= 約 23 cycle)。 従って
* SUB_ASYNC_FLUSH=0 の同期ビルドでは、 各所に入れたガードは完全な
* no-op であり、 従来動作と 1 bit も変わらない。
*
* 非同期バッチが飛んでいるときだけ:
*   1. BUSY が落ちるまで待つ (上限付き)
*   2. SUB_POST_WAIT ぶん保険待ちする
* を行い、 pending を落とす。
_sub_sync:
                tst     _sub_pending
                beq     .sy_done
                clr     _sub_pending    * 先に落とす (= 再入しても二重待ちしない)
                lbsr    _sub_wait_ready
                pshs    b
                ldb     #SUB_POST_WAIT
.sy_w:
                decb
                bne     .sy_w
                puls    b
.sy_done:
                rts

* void sub_halt(void) — HALT 要求 → BUSY=1 待ち (= HALT 完了確認)
*   wait READY (= 旧 bmi loop) は省略。 実機 HALT は要求即発行で安全、
*   かつ実機準拠エミュの _subBusy=true 残留と race して永久
*   hang する事象を避けるため。
_sub_halt:
                lda     #$80
                sta     IO_SUBCTRL
.bw:            lda     IO_SUBCTRL
                bpl     .bw
                rts

* void sub_release(void) — HALT 解除
_sub_release:
                clr     IO_SUBCTRL
                rts


* ---- TEST cmd ヘッダを $FC80 に書き、 X = $FC8B (subcmd 先) を返す
.build_header:
                ldx     #SHARED_RAM
                clr     ,x+             * $FC80
                clr     ,x+             * $FC81
                lda     #$3F
                sta     ,x+             * $FC82 TEST
                clr     ,x+             * $FC83-$FC8A (= 8 byte zero)
                clr     ,x+
                clr     ,x+
                clr     ,x+
                clr     ,x+
                clr     ,x+
                clr     ,x+
                clr     ,x+
                rts                     * X = $FC8B


* ---- TEST CALL 列を組んで RELEASE するところまで (= 待たない)
*   entry: D = addr
*   先頭で _sub_sync を通すので、 非同期バッチが飛んでいる最中に
*   HALT してしまう事故 (= サブが読んでいる最中のキュー上書き) は起きない。
.issue_call:
                pshs    a,b             * addr 退避 (halt/build_header が D を壊す)
                lbsr    _sub_sync       * 前の非同期バッチの完了待ち (同期時 no-op)
* wait_ready は省略 (= sub_halt が内包する BUSY 待ちで HALT 完了確認できる)。
* main 側 caller が release 直後 sub_call を呼ぶケースでは _subBusy=true
* が残ってて wait_ready が永久 hang する (= 実機準拠エミュ)。
                lbsr    _sub_halt
                lbsr    .build_header   * X = $FC8B
                lda     #$93
                sta     ,x+             * CALL
                puls    a,b             * addr 復帰
                std     ,x++            * addr
                lda     #$90
                sta     ,x              * END
                lbsr    _sub_release
                rts


* void sub_call(unsigned addr)  — 同期版
*   addr に JSR 相当で飛ぶ (= sub の RTS まで待ってから戻る)
*   stack: [ret:2] [addr:2]
_sub_call:
                pshs    u
                ldd     4,s             * addr
                lbsr    .issue_call
                lbsr    _sub_wait_busy  * まずサブが拾うのを待つ (= 即抜け防止)
                lbsr    _sub_wait_ready * そのうえで sub の RTS 完了を待つ
                puls    u,pc


* void sub_call_async(unsigned addr)  — 非同期版 (= 待たずに戻る)
*   RELEASE したらそのまま戻り、 メイン CPU はサブと並列に走る。
*   完了待ちは次に _sub_sync を通る誰か (= 次フレームの sub_flush、
*   sub_takeover、 subsys_halt、 sub_cancel) が肩代わりする。
*   stack: [ret:2] [addr:2]
_sub_call_async:
                pshs    u
                ldd     4,s             * addr
                lbsr    .issue_call
* 立ち上がりだけは待つ (= 実測 36-330 cycle、 中央値 36)。 ここを省くと
* 「サブがまだ拾っていない」 状態で pending を立てることになり、 次に
* sub_sync が見る BUSY が 「前のバッチのもの」 なのか 「まだ始まって
* いないだけ」 なのか区別できず、 完了待ちが無意味になる。
* 完了そのものは待たない (= 非同期の目的はここで戻ること)。
                lbsr    _sub_wait_busy
                lda     #1
                sta     _sub_pending    * 「投げっぱなしのバッチが有る」 印
                puls    u,pc


* void sub_queue_copy(const void *src, unsigned char *dst, unsigned n)
*   src から dst へ n byte コピーし、 dst[n] に $00 (= SUBCMD_END) を置く。
*   stack: [ret:2] [src:2] [dst:2] [n:2]
*
*   これは c_subprog.c の sub_flush() が持っていた C の for ループの置換で
*   ある。 CMOC が吐くコードは 1 byte あたり 69 cycle
*   (LDB/LEAX/ABX/LDB/PSHS/LDB/CLRA/ADDD/TFR/LDB/STB/INC/LDB/CMPB/BLO)
*   かかるのに対し、 ここは 1 byte あたり 17 cycle
*   (lda ,x+ 6 + sta ,y+ 6 + decb 2 + bne 3) で済む。
*   キュー 45 byte で 2,340 cycle ≒ 1.95ms の短縮になる。
*
*   ※ S をコピー用ポインタに転用してはならない。 サブ CPU の 20ms タイマ
*     NMI は禁止できず、 S の指す先にマシン状態 12 byte を書き込むため
*     データが壊れる。 X / Y / U のみを使うこと。
*   ※ n は 1..255 を前提 (= 実際は SUB_QUEUE_BYTES = 108 以下)。
*     上位 byte は見ない。
_sub_queue_copy:
                pshs    y
* stack: 0,s=saved Y(2) / 2,s=ret(2) / 4,s=src(2) / 6,s=dst(2) / 8,s=n(2)
                ldx     4,s             * src
                ldy     6,s             * dst
                ldb     9,s             * n の下位 byte
                beq     .qc_end
.qc:
                lda     ,x+
                sta     ,y+
                decb
                bne     .qc
.qc_end:
                clr     ,y              * キュー終端 (= SUBCMD_END $00)
                puls    y,pc


* void sub_takeover(const void *code, unsigned len, unsigned dst, unsigned exec)
*   main RAM 上の code[] を sub の dst へ len byte 転送、 終わったら
*   sub_call(exec) で実行開始。 共有 RAM 後半 $FCA0+ を chunk バッファ
*   (= 最大 64 byte / chunk) として使う。
*   stack: [ret:2] [code:2] [len:2] [dst:2] [exec:2]
*
*   ★ 先頭で _sub_sync を通すこと (= Phase 2 ガード)。 転送は共有 RAM
*     $FCA0+ を chunk バッファに使うので、 非同期で投げたバッチをサブが
*     まだ読んでいる最中に HALT + 上書きすると、 そのバッチが静かに壊れる。
*     症状は暴走ではなく 「1 フレーム描画が欠ける」 だけなので気付けない。
_sub_takeover:
                lbsr    _sub_sync       * 非同期バッチの完了待ち (同期時 no-op)
                pshs    u,y
                leas    -5,s
* 最終 stack (pshs u,y → leas -5,s 後):
*   0,s  = chunk (1B)
*   1,s  = w_dst (2B)
*   3,s  = w_remain (2B)
*   5,s  = saved Y (2B)
*   7,s  = saved U (2B)
*   9,s  = ret addr (2B)
*  11,s  = arg code (2B)
*  13,s  = arg len  (2B)
*  15,s  = arg dst  (2B)
*  17,s  = arg exec (2B)
                ldd     13,s            * len
                std     3,s             * w_remain = len
                ldd     15,s            * dst
                std     1,s             * w_dst = dst
                ldx     11,s            * code → keep in X across loop

.loop:
                ldd     3,s             * remain
                beq     .done
                cmpd    #64
                bls     .szok
                ldd     #64
.szok:
                stb     ,s              * chunk (1B、 最大 64 なので B のみで OK)

                lbsr    _sub_halt
* 1) chunk byte を $FCA0+ にコピー
                ldy     #SHARED_RAM+$20 * = $FCA0
                ldb     ,s
.cp:
                lda     ,x+
                sta     ,y+
                decb
                bne     .cp
* 2) TEST MOVE cmd 構築 (X は保持したいので別レジスタで一旦受け)
                stx     5,s             * save X = next src ptr
                lbsr    .build_header   * X = $FC8B
                lda     #$91
                sta     ,x+             * MOVE
                ldd     #SHARED_SUB     * src (sub から見た $D3A0)
                std     ,x++
                ldd     1,s             * w_dst
                std     ,x++
                clra
                ldb     ,s              * chunk
                std     ,x++            * len
                lda     #$90
                sta     ,x              * END
                lbsr    _sub_release
                lbsr    _sub_wait_busy  * サブが MOVE を拾うのを待つ
                lbsr    _sub_wait_ready * MOVE 完了を待つ
                ldx     5,s             * restore code ptr

* 3) w_dst += chunk, w_remain -= chunk
*
*   16-bit 加算は「ldd → addb → adca #0 → std」 の素朴な形で書く
*   こと (= 途中で clra を挟むと HI byte を消す罠あり)。
                ldd     1,s             * D = w_dst (16-bit)
                addb    ,s              * B += chunk (lo)
                adca    #0              * A += carry (hi)
                std     1,s             * w_dst += chunk
*
*   w_remain -= chunk: chunk を 16-bit に符号拡張 (= sex で A = $FF
*   全 byte に伸ばす) して 16-bit 加算で減算する。 chunk は 1-64 の
*   範囲なので negb で 2's complement の負数にしてから sex で OK。
                ldd     3,s
                clra
                ldb     ,s
                negb
                sex                     * D = -chunk (16-bit 符号拡張)
                addd    3,s
                std     3,s
                bra     .loop

.done:
                leas    5,s
                ldd     12,s            * exec (= 0 なら CALL skip = 転送のみ)
                beq     .skip
                pshs    b,a
                lbsr    _sub_call
                leas    2,s
.skip:
                puls    u,y,pc


* ---- 非同期バッチの在中フラグ --------------------------------------
* 「sub_call_async で投げたきり、 まだ完了待ちをしていないバッチが有る」
* を表す 1 byte。 _sub_call_async が立て、 _sub_sync が落とす。
*
* ★ bss ではなく code セクションに置いている。 asm_crt0.s の INILIB は
*   ダミー RTS で、 bss のゼロクリアを一切行わないため、 bss に置くと
*   起動時の値が不定になる (= 起動 1 発目のガードが誤発火し得る)。
*   code セクションは本体イメージごとディスクから RAM に展開されるので、
*   ここに置いた fcb 0 は確実に 0 で始まる。
_sub_pending    fcb     0

                end
