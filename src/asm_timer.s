* ============================================================
* asm_timer.s — メイン CPU の周期タイマ IRQ (約2ms) を数える
*               「経過 tick カウンタ」フレームペーシング (deadline 方式)
*
* 【設計】 deadline 方式:
*   - FM-7 メイン 6809 には約 2ms 周期 (≈491Hz) のタイマ IRQ がある
*     (要因フラグ $FD03 bit2、 許可は $FD02 bit2)。 これを IRQ ハンドラ
*     _irq_isr で数え、 16-bit カウンタ frame_tick を ++ する (= 自走する
*     経過 tick カウンタ。 1 tick ≈ 2ms)。
*   - メインループは「timer_start() で frame_tick を 0 に戻し、 timer_get()
*     が FRAME_TARGET に達するまでロック」 するだけ。 処理が重くても軽くても
*     1 フレームの実時間が FRAME_TARGET × 2ms に揃う (= IRQ は処理中も数え
*     続けるので、 ポーリングのような取りこぼしが無い)。
*
* 【なぜ IRQ か (FIRQ でなく)】
*   $FD04 bit1 の BREAK は FIRQ 要因。 前にアテンションを FIRQ で数えようと
*   して FIRQ を許可したら、 BREAK 押下中ずっと FIRQ が再発火してメインループ
*   が餓死した。 メインタイマは IRQ 要因 ($FD02/$FD03 bit2) なので、 IRQ だけ
*   許可し FIRQ はマスクのままにすれば、 BREAK と干渉しない (BREAK は従来どおり
*   break_check() が $FD04 bit1 をポーリング)。
*
* C API (c_subprog.h):
*   void     timer_init(void)   起動時 1 回。 IRQ ベクタ設置 + タイマ IRQ 許可。
*   void     timer_start(void)  経過カウンタ frame_tick を 0 に戻す。
*   unsigned timer_get(void)    timer_start() からの経過 tick (16-bit、 1≈2ms)。
* ペーシングはメインループ先頭で
*     while (timer_get() < FRAME_TARGET) {}   timer_start();
* と書く (= c_main 側。 ハング防止の安全キャップ付き)。
*
* ※ 機種依存 (実機/エミュで要検証):
*   ・IRQ ベクタ $FFF8 は IPL が RAM モード ($FD0F write) にして以降 RAM。
*   ・タイマ IRQ フラグ $FD03 bit2 は active-low (0=発生)。 $FD03 read で
*     ack される前提 (= クリアされないと IRQ が再発火し続ける)。
*   ・キーボード IRQ ($FD02 bit0) も許可し、 IRQ ハンドラでキーコードを
*     _kbd_buf に取り込む (= キー入力は IRQ 駆動)。
*
* 【ゲーム用入力モジュールとの連携 (asm_input.s)】
*   IRQ ハンドラは従来の _kbd_buf 更新に加えて、 次の 2 つを呼ぶ。
*     ・タイマ要因のとき  _input_tick     — BREAK のサンプリング (エッジ検出 +
*       チャタリング除去 + オート連射) と、 押下ビットのタイムアウト減衰。
*     ・キー要因のとき    _input_feed_key — キーコード (B) を方向保持状態と
*       押下ビットマップへ反映する。
*   従来の key_check() / break_check() はそのまま動く (共存)。
* ============================================================

IO_IRQFLAG      equ     $FD03           * R: IRQ 要因 (active-low。 bit2=タイマ, bit0=キーボード)
IO_IRQMASK      equ     $FD02           * W: IRQ 許可 (bit2=タイマ, bit0=キーボード)
IO_KEYDATA      equ     $FD01           * R: キーコード (= read でキーボード IRQ を ack)
TIMER_BIT       equ     $04             * $FD03/$FD02 bit2 = タイマ
KBD_BIT         equ     $01             * $FD03/$FD02 bit0 = キーボード
IRQ_VECTOR      equ     $FFF8           * 6809 IRQ ベクタ (RAM モードで RAM)

                section code

                export  _timer_init
                export  _timer_start
                export  _timer_get
                export  _timer_consume
                export  _irq_save
                export  _irq_restore
                import  _kbd_buf        * asm_kbd.s 側の bss (IRQ ハンドラが書く)
                import  _input_tick     * asm_input.s: 2ms ごとの BREAK サンプリング等
                import  _input_feed_key * asm_input.s: キーコード (B) を入力状態へ反映


* void timer_init(void) — IRQ ベクタを設置し、 タイマ IRQ を許可する (起動時 1 回)。
*   キーボード IRQ (bit0) も合わせて許可する (= キー入力を IRQ 駆動にするため)。
_timer_init:
                ldd     #_irq_isr
                std     IRQ_VECTOR      * $FFF8/$FFF9 に handler 番地
                lda     #TIMER_BIT+KBD_BIT  * bit2=タイマ + bit0=キーボード IRQ 許可
                sta     IO_IRQMASK
                andcc   #$EF            * I フラグ解除 = IRQ 許可 (F は立てたまま)
                rts


* void timer_start(void) — 経過 tick カウンタを 0 に戻す。
_timer_start:
                clr     frame_tick
                clr     frame_tick+1
                rts


* unsigned timer_get(void) — 経過 tick (16-bit) を D で返す。
*   IRQ が ++ 途中に割込む torn read はペーシングでは実害小なので許容。
_timer_get:
                ldd     frame_tick
                rts


* void timer_consume(unsigned n) — 経過 tick カウンタから n を差し引く。
*
*   フレームペーシングの締めで timer_start (= 0 リセット) の代わりに使う。
*   リセットは 「n を超えて経った時間」 を毎フレーム切り捨てるため、 締切を
*   少しでも過ぎたフレームの超過が全部そのまま fps の低下になる。 差し引き
*   なら超過が翌フレームへ持ち越され、 締切より早く終わったフレームが
*   その借りを返す (= 数フレーム平均が n 以内なら fps は落ちない)。
*   実測に基づいて導入した。
*
*   - 差し引いた残りが n を超えるときは n に丸める (= 借りは 1 フレーム
*     ぶんまで。 面切替の CLS など数フレーム級の停止の後で、 早回しの
*     追い掛けが何フレームも続くのを防ぐ)。
*   - n 未満しか経っていないとき (= ペーシングの保険 cap で抜けた場合)
*     は 0 にする (= 従来の timer_start と同じ)。
*   - IRQ ハンドラが frame_tick を触るので、 読み書きの間だけ I を立てる
*     (= torn write は 256 tick 境界で 0.5 秒級の狂いになり得るため許容
*     しない)。
_timer_consume:
                ldd     2,s             * n
                orcc    #$10            * IRQ を一時停止
                pshs    b,a             * n を作業用に退避
                ldd     frame_tick
                subd    ,s              * 経過 - n
                bpl     .tc_pos
                ldd     #0              * n 未満だった → 0 (= 従来と同じ)
                bra     .tc_store
.tc_pos:
                cmpd    ,s
                ble     .tc_store        * 余り <= n はそのまま持ち越す
                ldd     ,s              * 借りは 1 フレームぶんに丸める
.tc_store:
                std     frame_tick
                puls    a,b
                andcc   #$EF            * IRQ 再開
                rts


* unsigned char irq_save(void) — マスク前の CC を返して IRQ をマスクする。
*
*   irq_restore と組で使い、 途中で割込まれたくない短い I/O の列を囲む。
*   使いどころは c_sound.c の psg_write: FM77AV では PSG 互換口
*   ($FD0D/$FD0E) と FM 音源口 ($FD15/$FD16) が同一チップに繋がっていて
*   アドレスラッチを共有するため、 書込みの列の途中で IRQ 側の _joy_read
*   (本作では asm_input.s 末尾のスタブ) が走るとラッチが差し替わり、 別レジスタへ化けて書いてしまう。
*   戻り値: B = マスク前の CC (= そのまま irq_restore へ渡す)。
_irq_save:
                tfr     cc,b            * マスク前の CC を控える
                orcc    #$10            * IRQ マスク
                clra
                rts


* void irq_restore(unsigned char cc) — irq_save の控えどおりに IRQ を戻す。
*
*   ★ 無条件に許可へ倒してはいけない (呼び出し元が元々 IRQ マスク中かも
*     しれない)。 控えの I ビットが 0 だった時だけ許可へ戻す。
*   CMOC は char 引数を 16-bit へ昇格してスタックに積むので、 値 (下位) は
*   3,s に居る (= 生成コードで確認した)。
_irq_restore:
                ldb     3,s             * 控えた CC (昇格ワードの下位)
                andb    #$10
                bne     .ir_done        * 元からマスク中 → 触らない
                andcc   #$EF            * 元は許可 → 許可へ戻す
.ir_done:
                rts


* --- メイン IRQ ハンドラ (タイマ + キーボード) ---
*   IRQ は全レジスタを自動 push する (E=1) ので、 A/B/CC を自由に使って RTI で
*   復帰できる。 $FD03 を 1 回読み (= タイマ ack)、 タイマ(bit2)なら frame_tick++、
*   キーボード(bit0)なら $FD01 を読んで (= キーボード ack) _kbd_buf に格納する。
_irq_isr:
                lda     IO_IRQFLAG      * $FD03 read (= 要因判別 + タイマ ack)
                pshs    a               * 要因を退避 (以下の jsr が A を壊すため)
                bita    #TIMER_BIT
                bne     .chk_kbd        * bit2=1 → タイマでない
                inc     frame_tick+1    * 16-bit ++ (下位)
                bne     .tick_in
                inc     frame_tick      * 桁上げ (上位)
.tick_in:
                jsr     _input_tick     * BREAK サンプリング + オート連射 + 減衰
.chk_kbd:
                lda     ,s              * 退避した $FD03 の値を読み直す
                bita    #KBD_BIT
                bne     .ii_done        * bit0=1 → キーボードでない
                ldb     IO_KEYDATA      * $FD01 read (= キーボード ack、 B=キーコード)
                cmpb    #$FF
                beq     .ii_done        * バス浮き ($FF) は無視
                tstb
                beq     .ii_done        * $00 も無視 (= 「キー無し」 と同義)
                stb     _kbd_buf        * 直近キーを格納 (key_check が取り出す)
                jsr     _input_feed_key * B = キーコード → 方向保持/押下ビットへ反映
.ii_done:
                puls    a               * 退避した要因を捨てる
                rti


                section bss
frame_tick      rmb     2               * 16-bit 経過 tick (IRQ が ++、 1≈2ms)

                end
