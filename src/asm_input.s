* ============================================================
* asm_input.s — ゲーム用 入力モジュール (IRQ 駆動)
*
* 【この機種のキーボードの制約 — 設計の前提】
*   本機のキーボードは、 押されたキーの文字コードを $FD01 に流す方式である。
*   公式マニュアル・公刊の解説書で確認できる範囲では、 「キーを離した」 事を
*   知らせる通知 (ブレークコード) は BREAK キー以外には存在しない。
*   つまり通常キーは 「押された瞬間」 しか取れず、 「今押されているか」 を
*   正確に知る手段が無い。
*
*   そのため本モジュールは 2 段構えにする。
*
*   (A) 方向は 「状態保持型」 にする (= 主 API)
*       テンキー 8/2/4/6 を押すと方向が確定し、 キーを離してもその方向を
*       保持し続ける。 テンキー 5 で停止する。 離鍵通知が要らないので、
*       この方式ならこの機種で確実に動く。
*
*   (B) 生の押下ビットマップは 「直近に押された」 の意味に留める (= 補助 API)
*       離鍵が取れない以上、 真の 「押しっぱなし」 は表現できない。
*       押下イベントごとにビットを立て、 一定時間で自動的に落とす
*       (= RAW_HOLD タイムアウト)。 これは保険であり、 移動の判定に
*       使ってはならない。 移動には (A) の input_dir() を使うこと。
*
*   BREAK キーだけは例外で、 専用ステータス $FD04 bit1 に押下中ずっと
*   出続ける (active-low)。 よってショットは BREAK に割り当て、 周期タイマ
*   IRQ でサンプリングして押下エッジを数える + オート連射を生成する。
*
* 【ジョイスティック】
*   FM 音源ポート経由のジョイスティックは、 キーボードと違って **離した事が
*   判る**ので、 素直な 「倒している間だけ動く」 入力になる。
*   本モジュールとの繋ぎ方は 2 箇所だけである。
*
*   (1) 方向 — input_dir() で **ジョイスティックを優先**する
*         スティックが倒れていればその方向、 中立ならキーの保持方向。
*         キーだけで遊ぶ人は _joy_bits_var が常に 0 なので、 従来と
*         1 ビットも変わらない挙動になる。
*   (2) トリガ — BREAK のレベルに **論理和**して既存の状態機械へ流す
*         チャタリング除去もオート連射も BREAK 用のものがそのまま効くので、
*         ボタン押しっぱなしは BREAK 押しっぱなしと同じ連射になる。
*         新しい状態変数を持たないので、 二重計上も起こり得ない。
*
*   スティックの標本化は JOY_DIV tick に 1 回に間引く (下の定数を参照)。
*
* 【なぜ BREAK を FIRQ にしないか】
*   $FD04 bit1 はレベル出力なので、 FIRQ を許可すると押下中ずっと再発火し、
*   メインループが餓死する (= 実際に起きた)。 よって FIRQ はマスクのまま、
*   タイマ IRQ (約 2ms) の中でポーリングする。
*
* C API (c_input.h):
*   void          input_init(void)     起動時 1 回 (timer_init の後)。
*   unsigned char input_dir(void)      保持中の移動方向ビット (IN_UP 等)。
*   void          input_dir_clear(void) 方向を停止状態に戻す。
*   unsigned char input_state(void)    生の 「直近押下」 ビットマップ。
*   unsigned char input_edges(void)    前回呼出以降に押されたキーのビット和
*                                      (読むとクリア)。
*   unsigned char break_shots(void)    前回呼出以降のショット数 (読むとクリア)。
*   unsigned char break_held(void)     BREAK 押下中なら 1。
*
* ============================================================

IO_BREAK        equ     $FD04           * R: bit1 = BREAK (0 = 押下、 active-low)

* --- 入力ビット定義 (c_input.h の IN_* と必ず一致させること) ---
IN_UP           equ     $01
IN_DOWN         equ     $02
IN_LEFT         equ     $04
IN_RIGHT        equ     $08
IN_STOP         equ     $10
IN_FIRE         equ     $20
IN_PAUSE        equ     $40
IN_START        equ     $80

IN_VMASK        equ     $03             * 縦軸 (UP|DOWN)
IN_HMASK        equ     $0C             * 横軸 (LEFT|RIGHT)
IN_DIRMASK      equ     $0F             * 方向 4 ビット
* 上の各マスクのビット反転 (= 「この軸だけ落とす」 and マスク)。
* アセンブラの単項演算子に依存しないよう、 値を直接書く。
IN_NVMASK       equ     $FC             * = ~IN_VMASK   (縦軸だけ落とす)
IN_NHMASK       equ     $F3             * = ~IN_HMASK   (横軸だけ落とす)
IN_NDMASK       equ     $F0             * = ~IN_DIRMASK (方向 4 ビットを全部落とす)

* --- 調整用定数 ---
* BRK_DEBOUNCE: BREAK の状態変化を確定させるのに必要な連続一致 tick 数。
*   1 tick ≒ 2ms なので 3 tick ≒ 6ms。 機械接点のチャタリングは概ね
*   数 ms 以内に収まるのでこれで吸収でき、 かつ理論上 80 回/秒 まで
*   押下を数えられるので人間の連打 (速い人で 15 回/秒 程度) を制限しない。
BRK_DEBOUNCE    equ     3

* AUTO_PERIOD: オート連射の周期 (tick 数)。 1 tick ≒ 2.035ms。
*   30 tick ≒ 61ms ≒ 16.4 発/秒。 押しっぱなしでこのレートで撃つ。
*   人間の連打がこれを上回った場合は連打が優先される (下の合成規則を参照)。
AUTO_PERIOD     equ     30

* RAW_HOLD: 生押下ビットを立てておく時間 (減衰処理の呼出回数単位)。
*   減衰は DECAY_DIV tick に 1 回なので、 8 * 8 tick ≒ 130ms。
RAW_HOLD        equ     8
DECAY_DIV       equ     8               * 何 tick に 1 回 減衰処理を回すか

* JOY_DIV: ジョイスティックを読む間隔 (tick 数)。 1 tick ≒ 2.035ms なので
*   8 tick ≒ 16.3ms ≒ 61 回/秒 = 画面と同じ回数。 #45 で 1 回の標本化に
*   音源チップの BUSY 待ち (R15 の書込み + R14 の読出し、 約 300 cycle) が
*   入ったため、 4 tick (123 回/秒) から 8 tick へ間引いた。 オート連射の
*   周期 (30 tick ≒ 61ms) とチャタリング除去 (3 tick) に対して十分細かい。
JOY_DIV         equ     8

* トリガのビット (トリガ 1 / トリガ 2 のどちらでも拾う)。
JOY_TRIGS       equ     $30             * トリガ 1 / トリガ 2 のどちらでも

                section code

* ジョイスティックの実装は本作では外してある。 参照先はファイル
* 末尾のスタブ (_joy_init/_joy_read = 即 RTS、 _joy_bits_var = 常に 0)。
* スティック対応を入れるときはこのスタブを実装へ置き換えること。

                export  _input_init
                export  _input_dir
                export  _input_dir_clear
                export  _input_state
                export  _input_edges
                export  _break_shots
                export  _break_held
                export  _input_tick         * IRQ ハンドラ (asm_timer.s) が呼ぶ
                export  _input_feed_key     * IRQ ハンドラ (asm_timer.s) が呼ぶ


* ------------------------------------------------------------
* void input_init(void) — 内部状態を初期化する (起動時 1 回、 timer_init の後)
*   BREAK の初期レベルを今の実状態に合わせ、 起動直後に偽のショットが
*   1 発入るのを防ぐ。
* ------------------------------------------------------------
_input_init:
                pshs    cc
                orcc    #$10            * IRQ マスク (初期化中に tick が入らないように)
                clr     dir_state
                clr     raw_state
                clr     edge_state
                clr     brk_shots
                clr     brk_dbnc
                clr     tick_div
                clr     joy_div
                lda     #AUTO_PERIOD
                sta     brk_auto
                ldy     #raw_tmr
                ldb     #8
.iz             clr     ,y+
                decb
                bne     .iz
                * --- ジョイスティックの有無を判別する (Issue #23)。
                *     IRQ を止めた今のうちに済ませる。 音源カードが無ければ
                *     _joy_bits_var は 0 (= 中立) のまま据え置かれ、 以後
                *     キー入力だけで従来どおり動く。 判別にはループが無いので、
                *     刺さっていない環境でも待ち続けることは無い。
                jsr     _joy_init
                lda     IO_BREAK        * 今の BREAK レベルを初期状態にする
                anda    #$02
                beq     .ipressed       * 0 = 押されている (active-low)
                clr     brk_state
                bra     .idone
.ipressed       lda     #1
                sta     brk_state
.idone          puls    cc
                rts


* ------------------------------------------------------------
* unsigned char input_dir(void) — 移動方向ビットを返す
*   bit0=上 bit1=下 bit2=左 bit3=右。 斜めは 2 ビット同時に立つ。
*   1 バイト読みは 6809 の単一命令なので IRQ に割られない (マスク不要)。
*
*   【ジョイスティックが優先。 中立ならキーの保持方向 (Issue #23)】
*   ジョイスティックは離した事が判るので 「倒している間だけ」 が素直に取れる。
*   よってスティックが倒れていればそれをそのまま返し、 中立のときだけ
*   キーの保持方向へ落ちる。
*     ・ジョイスティックだけで遊ぶ人 … キーを触らないので dir_state は 0 の
*       まま。 手を放せば止まる、 という普通のシューティングの操作になる。
*     ・キーだけで遊ぶ人 … 音源カードが無ければ _joy_bits_var は常に 0 なので、
*       この 3 命令を素通りして従来と完全に同じ値が返る。
*   費用は ldb(5) + andb(2) + bne(3) = 10 cycle / 呼出。
* ------------------------------------------------------------
_input_dir:
                ldb     _joy_bits_var   * スティックの状態 (アクティブ High)
                andb    #IN_DIRMASK
                bne     .idjoy          * 倒れている → そちらを使う
                ldb     dir_state       * 中立 → キーの保持方向
.idjoy
                clra
                rts


* ------------------------------------------------------------
* void input_dir_clear(void) — 方向を停止状態に戻す (テンキー 5 と同じ効果)
* ------------------------------------------------------------
_input_dir_clear:
                clr     dir_state
                rts


* ------------------------------------------------------------
* unsigned char input_state(void) — 生の 「直近押下」 ビットマップを返す
*   ※ 離鍵が取れない機種なので、 これは 「押しっぱなし」 ではなく
*     「ごく最近押された」 の意味。 移動判定に使わないこと。
* ------------------------------------------------------------
_input_state:
                ldb     raw_state
                clra
                rts


* ------------------------------------------------------------
* unsigned char input_edges(void) — 前回呼出以降に押されたキーのビット和
*   読み出しと同時にクリアする (取りこぼしの無いエッジ検出)。
*   read + clear を atomic にするため IRQ を一瞬マスクする。
* ------------------------------------------------------------
_input_edges:
                pshs    cc
                orcc    #$10
                ldb     edge_state
                clr     edge_state
                puls    cc
                clra
                rts


* ------------------------------------------------------------
* unsigned char break_shots(void) — 前回呼出以降に発生したショット数
*   (人間の押下エッジ + オート連射の合計)。 読み出すとクリアする。
*   255 で飽和する (= wrap して 0 に戻らない)。
* ------------------------------------------------------------
_break_shots:
                pshs    cc
                orcc    #$10
                ldb     brk_shots
                clr     brk_shots
                puls    cc
                clra
                rts


* ------------------------------------------------------------
* unsigned char break_held(void) — BREAK 押下中なら 1 (チャタリング除去済)
* ------------------------------------------------------------
_break_held:
                ldb     brk_state
                clra
                rts


* ------------------------------------------------------------
* void input_tick(void) — 周期タイマ IRQ (約 2ms) から呼ばれる
*
*   1. BREAK をサンプリングし、 チャタリング除去 → 押下エッジを数える
*      → 押しっぱなしならオート連射を積む
*   2. DECAY_DIV tick に 1 回、 生押下ビットのタイムアウト減衰を行う
*
*   【人間の連打とオート連射の合成】
*     押下エッジが立った瞬間に「1 発足す」 と同時に brk_auto を
*     AUTO_PERIOD へ 「巻き戻す」。 よって
*       ・連打が AUTO_PERIOD より速い → オートが満了する前に毎回巻き戻る
*         ので、 オートは 1 発も撃たず 「連打の回数そのもの」 が出る (連打優先)。
*       ・押しっぱなし / 連打が遅い → オートが満了して AUTO_PERIOD ごとに
*         1 発ずつ積まれる (下限が保証される)。
*     二重計上は起きない。
* ------------------------------------------------------------
_input_tick:
                * --- ジョイスティックを JOY_DIV tick に 1 回だけ読む (#23) ---
                *     音源カードが無ければ _joy_read は入口で返るだけで、
                *     _joy_bits_var は 0 (= 中立) のまま動かない。
                inc     joy_div
                lda     joy_div
                cmpa    #JOY_DIV
                blo     .jskip
                clr     joy_div
                jsr     _joy_read
.jskip
                * --- 発射のレベルを読む (A = 0:離 / 1:押) ---
                *     BREAK キー **または** ジョイスティックのトリガ。
                *     ここで 1 本にまとめてしまえば、 以降のチャタリング除去・
                *     押下エッジ・オート連射は BREAK 用の物がそのまま効く。
                *     状態変数を増やさないので二重計上も起こり得ない。
                lda     IO_BREAK
                anda    #$02
                beq     .braw1          * 0 = BREAK 押下 (active-low)
                lda     _joy_bits_var   * BREAK は離れている → トリガを見る
                anda    #JOY_TRIGS
                bne     .braw1          * トリガ 1 / 2 のどちらかが押されている
                clra
                bra     .bcmp
.braw1          lda     #1
.bcmp
                cmpa    brk_state
                bne     .bchg
                clr     brk_dbnc        * 現状と一致 → チャタリング計数をリセット
                bra     .bauto
.bchg
                inc     brk_dbnc        * 現状と違う → 連続一致数を数える
                ldb     brk_dbnc
                cmpb    #BRK_DEBOUNCE
                blo     .bauto          * まだ確定しない
                clr     brk_dbnc
                sta     brk_state       * 新しい状態を確定
                tsta
                beq     .bdecay         * 離した確定 → 何も撃たない
                bsr     .addshot        * 押した確定 = 立ち上がりエッジ → 1 発
                lda     #AUTO_PERIOD
                sta     brk_auto        * オート連射の位相を巻き戻す
                bra     .bdecay
.bauto
                lda     brk_state
                beq     .bdecay         * 離しているならオート連射しない
                dec     brk_auto
                bne     .bdecay
                bsr     .addshot        * オート連射 1 発
                lda     #AUTO_PERIOD
                sta     brk_auto

                * --- 生押下ビットの減衰 (DECAY_DIV tick に 1 回) ---
.bdecay
                inc     tick_div
                lda     tick_div
                cmpa    #DECAY_DIV
                blo     .tdone
                clr     tick_div
                ldy     #raw_tmr
                ldb     #$01            * B = 対象ビットのマスク
.dl             lda     ,y
                beq     .dnext          * 既に 0 → そのビットは落ちている
                deca
                sta     ,y
                bne     .dnext
                pshs    b               * 0 になった → raw_state の該当ビットを落とす
                comb
                andb    raw_state
                stb     raw_state
                puls    b
.dnext          leay    1,y
                lslb                    * 次のビットへ (8 回で 0 になり抜ける)
                bne     .dl
.tdone
                rts

* ショット数を 1 増やす (255 で飽和)。 _input_tick からのみ呼ぶ。
.addshot
                lda     brk_shots
                cmpa    #$FF
                beq     .as_done
                inca
                sta     brk_shots
.as_done
                rts


* ------------------------------------------------------------
* void input_feed_key(void) — B = 受信したキーコード。 IRQ ハンドラが呼ぶ
*
*   keytab を引いて、 方向の保持状態 (dir_state) と 生押下ビット
*   (raw_state / edge_state) を更新する。
*
*   方向の更新は 「軸ごとの置き換え」 で行う:
*     dir_state = (dir_state & andmask) | ormask
*   縦の指示 (8/2) は縦軸だけを、 横の指示 (4/6) は横軸だけを書き換えるので、
*   8 を押した後に 6 を押せば自然に 「右上」 (2 ビット同時) になる。
*   反対方向 (8 の後に 2 など) は同じ軸を書き換えるので 「後から入った方が
*   勝つ」 (相殺して停止にはしない)。
* ------------------------------------------------------------
_input_feed_key:
                stb     feed_code
                ldx     #keytab
.fl             lda     ,x
                beq     .fdone          * テーブル終端 (code = 0)
                cmpa    feed_code
                beq     .fhit
                leax    4,x
                bra     .fl
.fhit
                lda     dir_state
                anda    1,x             * 対象の軸をクリア
                ora     2,x             * 新しい方向を立てる
                sta     dir_state
                lda     3,x             * 生押下ビット
                sta     feed_raw
                ora     raw_state
                sta     raw_state
                lda     feed_raw
                ora     edge_state
                sta     edge_state
                * 立てたビットの recency タイマを満充填する
                ldy     #raw_tmr
                ldb     #8
.rt1            lsr     feed_raw
                bcc     .rt2
                lda     #RAW_HOLD
                sta     ,y
.rt2            leay    1,y
                decb
                bne     .rt1
.fdone
                rts


* ------------------------------------------------------------
* キーコード変換テーブル
*   1 エントリ 4 byte: [キーコード, dir の and マスク, dir の or マスク, 生押下ビット]
*   キーコード 0 で終端。
*
*   この機種のキーボードは押されたキーの文字コードを返すので、
*   テンキーの数字は普通の数字と同じコードで来る ('8' = $38 など)。
*   カーソルキーは制御コード $1C-$1F に割り当てられている。
* ------------------------------------------------------------
keytab
                * --- テンキー 8/2/4/6 = 上/下/左/右 (主操作) ---
                fcb     $38,IN_NVMASK,IN_UP,IN_UP               * '8' 上
                fcb     $32,IN_NVMASK,IN_DOWN,IN_DOWN           * '2' 下
                fcb     $34,IN_NHMASK,IN_LEFT,IN_LEFT           * '4' 左
                fcb     $36,IN_NHMASK,IN_RIGHT,IN_RIGHT         * '6' 右
                * --- テンキー 5 = 停止 (方向 4 ビットを全部落とす) ---
                fcb     $35,IN_NDMASK,$00,IN_STOP               * '5' 停止
                * --- テンキー 7/9/1/3 = 斜めの直接指定 (両軸を同時に置換) ---
                fcb     $37,IN_NDMASK,IN_UP+IN_LEFT,IN_UP+IN_LEFT       * '7' 左上
                fcb     $39,IN_NDMASK,IN_UP+IN_RIGHT,IN_UP+IN_RIGHT     * '9' 右上
                fcb     $31,IN_NDMASK,IN_DOWN+IN_LEFT,IN_DOWN+IN_LEFT   * '1' 左下
                fcb     $33,IN_NDMASK,IN_DOWN+IN_RIGHT,IN_DOWN+IN_RIGHT * '3' 右下
                * --- カーソルキー (代替操作) ---
                fcb     $1E,IN_NVMASK,IN_UP,IN_UP               * カーソル 上
                fcb     $1F,IN_NVMASK,IN_DOWN,IN_DOWN           * カーソル 下
                fcb     $1D,IN_NHMASK,IN_LEFT,IN_LEFT           * カーソル 左
                fcb     $1C,IN_NHMASK,IN_RIGHT,IN_RIGHT         * カーソル 右
                * --- 方向以外 (dir は変えない = and $FF / or $00) ---
                fcb     $20,$FF,$00,IN_FIRE                     * SPACE 発射 (予備)
                fcb     $70,$FF,$00,IN_PAUSE                    * 'p' ポーズ
                fcb     $50,$FF,$00,IN_PAUSE                    * 'P' ポーズ
                fcb     $0D,$FF,$00,IN_START                    * RETURN 開始/決定
                fcb     $00,$00,$00,$00                         * 終端


* ---- ジョイスティック無し構成のスタブ ----------------------------
* _joy_bits_var は常に 0 (= 中立) なので、 input_dir / _input_tick の
* スティック優先経路は 1 ビットも効かず、 キーボードの保持方向がそのまま
* 出る。 code セクションの fcb 0 なので起動時から確実に 0 である。
_joy_init:
_joy_read:
                rts
_joy_bits_var   fcb     0

                section bss
dir_state       rmb     1               * 保持中の移動方向 (IN_UP/DOWN/LEFT/RIGHT)
raw_state       rmb     1               * 生の 「直近押下」 ビットマップ
edge_state      rmb     1               * 前回 input_edges() 以降の押下ビット和
feed_code       rmb     1               * _input_feed_key の作業用 (受信コード)
feed_raw        rmb     1               * _input_feed_key の作業用 (ビット走査)
raw_tmr         rmb     8               * 生押下ビットごとの残り時間 (bit0 が先頭)
tick_div        rmb     1               * 減衰処理の分周カウンタ
joy_div         rmb     1               * ジョイスティック標本化の分周カウンタ
brk_state       rmb     1               * BREAK の確定状態 (0 = 離 / 1 = 押)
brk_dbnc        rmb     1               * BREAK チャタリング除去の連続一致数
brk_auto        rmb     1               * オート連射までの残り tick
brk_shots       rmb     1               * 未回収のショット数 (255 で飽和)

                end
