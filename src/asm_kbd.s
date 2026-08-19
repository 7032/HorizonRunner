* ============================================================
* asm_kbd.s — メイン CPU 側キーボード入力 (IRQ 駆動)
*
* FM-7 のキーボードはサブ CPU が scan して結果を $FD01 に流す方式。
* キーボード IRQ ($FD02 bit0) を有効にしないとデータが来ず、 また CPU IRQ を
* 許可する (= フレームペーシングで IRQ を解禁する) とキーボード IRQ も発生し、
* そのフラグは $FD01 を読むまでクリアされない。 そこで本テンプレでは:
*
*   - メインタイマと共用の IRQ ハンドラ (asm_timer.s の _irq_isr) が、
*     キーボード IRQ ($FD03 bit0=0) で $FD01 を読み (= IRQ を ack)、 キーコードを
*     共有バッファ _kbd_buf に格納する。
*   - key_check() は _kbd_buf を取り出して返すだけ (取り出しは IRQ を一瞬マスク
*     して read+clear を atomic に)。
*
* キーリピートは サブシステム ROM 側でも生成されるので、 押しっぱなしでも
* キーボード IRQ が繰り返し入り _kbd_buf が更新される。 雛形では main 側で
* direction 変数に保持して連続移動を実現する。
*
* テンキー → ASCII 対応:
*   '8' = $38 (UP)、 '2' = $32 (DOWN)、 '4' = $34 (LEFT)、 '6' = $36 (RIGHT)
*
* C API:
*   void          kb_init(void)
*     起動時に 1 回。 $FD02 bit0 = 1 でキーボード IRQ を有効化する (実際は後続の
*     timer_init() が $FD02 を $05 に再設定し CPU IRQ も解禁する)。
*
*   unsigned char key_check(void)
*     直近に押されたキーの ASCII (1..127) を返し、 無ければ 0。 連打/リピートは
*     ROM 任せで、 1 呼び出し 1 戻り値。 実体は _kbd_buf の取り出し (上記)。
* ============================================================

IO_IRQMASK      equ     $FD02           * W: IRQ マスク (= bit0 でキーボード IRQ enable)
IO_IRQFLAG      equ     $FD03           * R: IRQ フラグ (= bit0 = 0 でキーデータあり)
IO_KEYDATA      equ     $FD01           * R: キーコード (= read で IRQ 自動 clear)
IO_BREAK        equ     $FD04           * R: bit1 = BREAK キー (0 = 押下、 active-low)
IO_PALETTE      equ     $FD38           * W: パレット ($FD38-$FD3F = 色番号 0-7)
                                        *    値 = G*4 + R*2 + B (= デジタル GRB)

                section code

                export  _kb_init
                export  _key_check
                export  _break_check
                export  _palette_init
                export  _palette_page
                export  _kbd_buf        * IRQ ハンドラ (asm_timer.s) が書き込む


* void kb_init(void) — キーボード IRQ を有効化
*   ※ メインループでは asm_timer.s の timer_init() が $FD02 を $05
*     (キーボード bit0 + タイマ bit2) に設定し直し、 CPU IRQ も許可する。
*     以後キー入力は IRQ 駆動 (= ハンドラが $FD01 を読み _kbd_buf に格納)。
_kb_init:
                lda     #$01
                sta     IO_IRQMASK
                rts


* unsigned char key_check(void) — 直近に押されたキーの ASCII か 0 を返す
*
* キーボードは IRQ 駆動: asm_timer.s の IRQ ハンドラがキーボード IRQ で
* $FD01 を読み (= IRQ flag を ack)、 _kbd_buf に格納する。 ここではそれを
* 取り出して 0 に戻すだけ (= 1 イベント 1 戻り値、 連打は ROM のリピート任せ)。
* IRQ との read+clear 競合を避けるため、 取り出しの間だけ IRQ をマスクする。
* CMOC の戻り値規約: 8-bit は B、 上位 A は 0。
_key_check:
                pshs    cc
                orcc    #$10            * IRQ マスク (= _kbd_buf の取り出しを atomic に)
                ldb     _kbd_buf        * B = キーコード (0 = なし)
                clr     _kbd_buf
                puls    cc              * IRQ マスク状態を復元
                clra
                rts


* unsigned char break_check(void) — BREAK 押下中なら 1、 でなければ 0
*
* FM-7 の BREAK キーは通常のキー入力 ($FD01) には流れず、 専用の
* ステータス $FD04 bit1 に出る (= 0 が押下、 active-low)。 これを
* 読むだけ (= 読み取りに副作用なし、 ポーリングで毎フレーム呼べる)。
* メインループ側で「前回 0・今回 1」 の立ち上がりを見て 1 回の押下を
* 検出する (= c_main.c の brk_prev)。
_break_check:
                lda     IO_BREAK
                anda    #$02            * bit1 だけ見る
                beq     .pressed        * 0 = 押されている (active-low)
                clrb                    * 押されてない → 0
                clra
                rts
.pressed:
                ldb     #1              * 押下 → 1
                clra
                rts


* void palette_init(void) — 論理色番号 → 物理色 を割り当てる
*
* 本作のモノクロ 1 プレーン方式の要。 描画に使うのは B plane だけで、
* 色番号の bit0 が B plane の値になる。 そこで
*   偶数色番号 (= bit0 が 0、 B plane が 0) → 黒
*   奇数色番号 (= bit0 が 1、 B plane が 1) → 白
* と設定する。 こうすると **R/G plane に何が残っていても表示は B plane
* だけで決まる 2 値モノクロ** になるので、 起動時に R/G plane (32 KB) を
* クリアする必要がまるごと無くなる。
*
*   色番号: 0  1  2  3  4  5  6  7
*   物理色: 黒 白 黒 白 黒 白 黒 白
*   GRB値 : 0  7  0  7  0  7  0  7   (= G*4 + R*2 + B)
*   ※ パレットは書込専用なので read-modify-write (clr 等) は使わず、
*     必ず sta で書く。
_palette_init:
                ldx     #IO_PALETTE
                ldb     #4              * 黒/白 のペアを 4 組
.pal_loop:
                clra
                sta     ,x+             * 偶数色番号 = 黒
                lda     #$07
                sta     ,x+             * 奇数色番号 = 白
                decb
                bne     .pal_loop
                rts


* void palette_page(unsigned char page) — 表示するプレーンを選ぶ (ダブルバッファリング)
*
* 本作は **B plane と R plane を 2 枚の画面として使い分ける**。描いている
* 最中の絵を見せないためのダブルバッファリングであり、パレットを差し替えるだけで
* 表示面が入れ替わる (= VRAM の中身は 1 byte も動かさない)。
*
*   page 0 → 色番号の bit0 (= B plane) だけが白黒を決める
*   page 1 → 色番号の bit1 (= R plane) だけが白黒を決める
*
* CMOC の引数規約: 8-bit の第 1 引数は B に載って来る。
* パレットは書込専用なので必ず sta で書く (read-modify-write をしない)。
_palette_page:
                tstb
                bne     .pp_r
                ldx     #pal_b
                bra     .pp_go
.pp_r:
                ldx     #pal_r
.pp_go:
                ldy     #IO_PALETTE
                ldb     #8
.pp_loop:
                lda     ,x+
                sta     ,y+
                decb
                bne     .pp_loop
                rts

* ------------------------------------------------------------
* 3 枚のプレーンの役割
* ------------------------------------------------------------
*   B plane / R plane … ダブルバッファリングの表と裏。**自機 / 自弾 / 敵を白で描く**
*   G plane           … **地平線と床のカラープレーン (緑)**。ダブルバッファリングしない層で、
*                       白の面が黒い所にだけ透けて見える
*
* 色番号は bit0 = B / bit1 = R / bit2 = G。設定値は G*4 + R*2 + B なので
* $07 = 白 / $04 = 緑 / $00 = 黒。
*
*   表示中の面のビットが 1     → 白 (前景が最優先)
*   表示中の面が 0 で G が 1   → 緑 (地平線と床が透ける)
*   どちらも 0                 → 黒
*
* 裏の面のビットは色に一切効かない。これが「描いている最中の絵が見えない」
* ダブルバッファリングの実体である。
* 色番号        0   1   2   3   4   5   6   7
*   (G,R,B)   000 001 010 011 100 101 110 111
pal_b           fcb     $00,$07,$00,$07,$04,$07,$04,$07   * B plane を表示
pal_r           fcb     $00,$00,$07,$07,$04,$04,$07,$07   * R plane を表示


                section bss
* 直近に押されたキーの ASCII (0 = なし)。 IRQ ハンドラ (asm_timer.s) が
* キーボード IRQ で $FD01 を読んで格納し、 key_check() が取り出してクリアする。
_kbd_buf        rmb     1

                end
