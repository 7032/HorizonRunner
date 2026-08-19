* ============================================================
* asm_subprog.s — サブ CPU 上で動く描画プログラム (HorizonRunner)
*
* main から TEST MOVE で sub の SUB_PROG_ADDR ($C100) へ転送され、
* sub_call($C100) で実行される。1 回の呼び出しで共有 RAM に積まれた
* コマンド列を先頭から終端まで全部処理して RTS で main に戻る。
*
* ------------------------------------------------------------
* 三枚のプレーンの役割
* ------------------------------------------------------------
*   B plane ($0000-) … ダブルバッファリングの面 0。**白**。自機 / 自弾 / 敵を描く
*   R plane ($4000-) … ダブルバッファリングの面 1。**白**。同上
*   G plane ($8000-) … **緑のカラープレーン**。地平線と流れる床を描く (ダブルバッファリングしない)
*
* パレットは「表示中の面が 1 なら白、0 でも G が 1 なら緑、どちらも 0 なら
* 黒」に潰してある (src/asm_kbd.s)。よってキャラクタを描く面の背景は常に
* 真っ黒であり、**絵は OR で貼るだけでよい (マスクが要らない)**。
*
* ------------------------------------------------------------
* 描画枠は 384x96 dot (= 横 48 byte x 縦 96 line)
* ------------------------------------------------------------
* FM-7 の 640x200 は画素が縦長 (見かけの縦横比 約 1:2.4) なので、384x96 で
* ようやく画面上で横長に見える。枠は画面中央に置き、左上 = 52 line 目 /
* 16 byte 目 = 52*80+16 = $1050。行送りは +80。
*
* ------------------------------------------------------------
* 床の塗りは PSHU で 1 行 48 byte を 8 命令で
* ------------------------------------------------------------
* PSHU は U を減らしながら複数レジスタを書き出す命令で、5 + n cycle で
* n byte 書ける。D/X/Y の 3 本 = 6 byte を 11 cycle、つまり 1 byte 約 1.83
* cycle であり、STD の 4 cycle/byte の半分以下。枠幅 48 byte は 6 の倍数な
* ので端数無しにちょうど 8 回で埋まる。さらに床は **1 行おきに間引いて**
* 塗るので、実質 48 line ぶんの塗りで 96 line の枠が埋まる。
*
*   PSHU が書く順序 (低位→高位) は A,B,Xhi,Xlo,Yhi,Ylo。D/X/Y に「左から
*   右へ 6 byte ぶんの模様」を入れておけばよい。U は行の**右端の次**を
*   指した状態で始め、8 回押すと行頭に戻る。
*
*   ★ fill_rows は A も模様の一部として使う。行数を lda で設定してから
*     D を作ると 6 byte の先頭 1 byte だけが化ける。行数の設定は先に。
*
* ------------------------------------------------------------
* 消し込みはサブ側が自分で憶える
* ------------------------------------------------------------
* ダブルバッファリングの裏面には 2 フレーム前の絵が残っている。床は G plane にあって
* 毎フレーム塗り直すので勝手に消えるが、**面に描いたキャラクタは誰も
* 消さない**。そこでキャラクタを描くたびにサブ側が「面ごとの消し込み表」
* へ矩形を積み、次にその面が選ばれた時 ($07 PAGE) にまとめて黒で潰す。
* main から消し込みコマンドを送らずに済むので、共有 RAM のキュー
* (実効 108 byte) を情景と描画だけに使える。
*
* 通信プロトコル (= sub 側 $D393 から始まるコマンドキュー):
*   [cmd][params...] ... [$00]
*   $00 END     —                     キュー終端
*   $01 CLS     p                     1 面を全消去 (p = 0 B / 1 R / 2 G)
*   $04 SCENE   ct,cn,hz,n,[cnt,pat]xn  G plane に地平線と床を描く
*                 ct  : 空へ戻す枠内 line (偶数) / cn : その行数 (間引き後)
*                 hz  : 地平線の枠内 line (偶数)
*                 n   : 床の帯の数 / cnt : 帯の行数 (間引き後) / pat : 模様番号
*   $05 SPRITE  x,y,ph                自機を貼る (x=byte, y=line, ph=0-7)
*   $07 PAGE    p                     描画先の面を選び、その面を消し込む
*   $09 OBJS    n,[x,y,k|ph<<6]xn      敵 / 地上物 / 岩 / 自弾 / 爆発を n 個
*                 まとめて貼る (1 個 3 byte。k=絵の種類, ph=0-3)。
*                 キューは 108 byte しか無く、1 フレームに 2 回サブを呼ぶと
*                 呼出しの往復だけで約 2ms 失う (実測 57→51fps)。絵は全部
*                 この 1 命令に詰めて 1 回の呼出しに収める。
*   $0A WRITE   ah,al,n,[byte]xn      サブ側の番地 ah:al へ n byte 書く
*                 (書き手はこのサブ CPU 自身。起動時の絵の転送と、main 側に
*                 しか無いフォントでの文字描画に使う。毎フレームは使わない)
*
*   ($06 RECT / $08 ENEMY は OBJS に置き換わり、置場を空けるため撤去した)
* ============================================================

* ---- アドレス定数 (config.mk と要一致。check_layout.py が検査する) ----
SUB_WORK_ADDR   equ     $C000
SUB_TABLE_ADDR  equ     $C020
SUB_PROG_ADDR   equ     $C100
SUB_CODE_END    equ     $CF80

VRAM_GATE       equ     $D409
CMD_QUEUE       equ     $D393           * = main 側 $FC93
LINE_BYTES      equ     80
VRAM_DISP_END   equ     16000           * 200 line x 80 byte
GPLANE          equ     $8000           * 緑のカラープレーン (床と地平線)

* ---- 描画枠 (= 384x96 dot) ----
FRAME_TL        equ     52*LINE_BYTES+16        * $1050
FRAME_W_BYTES   equ     48
FRAME_H_LINES   equ     96
ROW_ADV         equ     LINE_BYTES*2+FRAME_W_BYTES  * 208 (= 2 行先の右端の次)

* ---- 面ごとの消し込み表 (SUB_TABLE_ADDR に置く) ----
* 自機 1 + OBJS 25 件が上限なので 26 件持つ。
LIST_MAX        equ     26              * 1 面あたりの矩形の数
LIST0           equ     SUB_TABLE_ADDR              * $C020-$C087
LIST1           equ     SUB_TABLE_ADDR+LIST_MAX*4   * $C088-$C0EF

* ---- 作業変数 (= 直接ページは使わず拡張アドレスで触る) ----
w_cnt           equ     SUB_WORK_ADDR+0   * 塗る行数 / 汎用カウンタ
w_vram          equ     SUB_WORK_ADDR+1   * 次に塗る行の「右端の次」 (2)
w_rows          equ     SUB_WORK_ADDR+3   * 枠内の残り行数
w_sx            equ     SUB_WORK_ADDR+4   * 一時置場
w_qsv           equ     SUB_WORK_ADDR+5   * キューポインタの退避 (2)
w_ct            equ     SUB_WORK_ADDR+7   * 空へ戻す開始 line
w_cn            equ     SUB_WORK_ADDR+8   * その行数
w_hz            equ     SUB_WORK_ADDR+9   * 地平線 line
w_nband         equ     SUB_WORK_ADDR+10  * 残りの帯の数
w_base          equ     SUB_WORK_ADDR+11  * 描画先の面の先頭 (2)
w_tmp           equ     SUB_WORK_ADDR+13  * 模様番号などの一時置場
w_page          equ     SUB_WORK_ADDR+14  * いま選ばれている面 (0/1)
w_ln0           equ     SUB_WORK_ADDR+15  * 面 0 の消し込み表の件数
w_ln1           equ     SUB_WORK_ADDR+16  * 面 1 の消し込み表の件数
w_rx            equ     SUB_WORK_ADDR+17  * fill_rect / reg_rect の x (byte)
w_ry            equ     SUB_WORK_ADDR+18  * 同 y (line)
w_rw            equ     SUB_WORK_ADDR+19  * 同 w (byte)
w_rh            equ     SUB_WORK_ADDR+20  * 同 h (line)
w_ew            equ     SUB_WORK_ADDR+21  * 敵の幅 (byte)
w_val           equ     SUB_WORK_ADDR+22  * fill_rect が塗る値
w_pair          equ     SUB_WORK_ADDR+23  * fill_rect の 2 byte 単位の残り
w_nobj          equ     SUB_WORK_ADDR+24  * OBJS の残り個数

                org     SUB_PROG_ADDR

* ---- エントリ (= sub_call が飛んでくる先) --------------------------
entry:
                lda     VRAM_GATE       * VRAM gate OPEN (read で開く)
                ldu     #CMD_QUEUE      * ← gate が開くまでの間の 1 命令
qloop:
                lda     ,u+
                beq     qend            * $00 END → 終わり
                cmpa    #$01
                lbeq    cmd_cls
                cmpa    #$04
                lbeq    cmd_scene
                cmpa    #$05
                lbeq    cmd_sprite
                cmpa    #$07
                lbeq    cmd_page
                cmpa    #$09
                lbeq    cmd_objs
                cmpa    #$0A
                lbeq    cmd_write
                                        * 未知コマンド → 即終了 (暴走させない)
qend:
                sta     VRAM_GATE       * VRAM gate CLOSE (write で閉じる)
                rts

* ---- $01 CLS p: 1 面を全消去 (p = 0 B / 1 R / 2 G) ------------------
* G plane を消し忘れると、起動直後は BASIC の残骸が緑で散ったままになる。
*
* ★ 3 面まとめて消してはならない。16,000 byte の消去はゲートを開けた
*   サブでは約 190ms 掛かり、3 面続けると main 側の完了待ち上限
*   (asm_test.s の SUB_WAIT_CAP) を超える。超えると main は「終わった」
*   と思って次のバッチを書き込み、**消去中のサブがそれを踏み潰す**
*   (症状: 消去の直後に描いた絵が虫食いになる)。面ごとに 1 命令へ割り、
*   1 回の呼び出しを上限内に収める。
cmd_cls:
                ldb     ,u+             * 消す面 (0 = B / 1 = R / 2 = G)
                ldx     #0
                cmpb    #1
                blo     .cl_go          * 0 = B 面
                beq     .cl_r           * ★ ldx はフラグを壊す。分岐を先に
                ldx     #GPLANE
                bra     .cl_go
.cl_r:
                ldx     #$4000
.cl_go:
                ldy     #VRAM_DISP_END/2
                ldd     #0
.c1:
                std     ,x++
                leay    -1,y
                bne     .c1
                clr     w_ln0
                clr     w_ln1
                lbra    qloop

* ============================================================
* fill_rows — D/X/Y の 6 byte 模様で、枠幅 48 byte の行を w_cnt 行塗る
*   entry: U = 塗る先頭行の「右端の次」/ D,X,Y = 6 byte の模様
*          w_cnt = 行数 (1 以上)
*   exit : U = 塗り終えた次の行の「右端の次」/ D,X,Y は保存される
*   ※ 1 行塗るごとに 2 line 進む (= 間引き)
* ============================================================
fill_rows:
                pshu    d,x,y           * 48 byte = 6 byte x 8 回
                pshu    d,x,y
                pshu    d,x,y
                pshu    d,x,y
                pshu    d,x,y
                pshu    d,x,y
                pshu    d,x,y
                pshu    d,x,y
                leau    ROW_ADV,u
                dec     w_cnt
                bne     fill_rows
                rts

* ============================================================
* row_addr — 枠内 line 番号 (B) から G plane 上の「その行の右端の次」
*   entry: B = 枠内 line (0-95)
*   exit : X = GPLANE + FRAME_TL + B*80 + 48
* ============================================================
row_addr:
                lda     #LINE_BYTES
                mul                     * D = line * 80
                addd    #GPLANE+FRAME_TL+FRAME_W_BYTES
                tfr     d,x
                rts

* ---- $04 SCENE ct,cn,hz,n,[cnt,pat]xn ------------------------------
* 地平線と床を G plane (緑) に描く。空 (地平線より上) には何も描かない。
cmd_scene:
                ldb     ,u+             * ct (空へ戻す開始 line)
                stb     w_ct
                ldb     ,u+             * cn (その行数)
                stb     w_cn
                ldb     ,u+             * hz (地平線 line)
                stb     w_hz
                ldb     ,u+             * n  (帯の数)
                stb     w_nband
                stu     w_qsv           * 以後 U は PSHU 用に使う

* (1) 空へ戻す (= 地平線が下がって、床だった行が空になった分)
                ldb     w_cn
                beq     .sc_hz
                stb     w_cnt
                ldb     w_ct
                lbsr    row_addr
                tfr     x,u
                ldd     #0
                tfr     d,x
                tfr     d,y
                lbsr    fill_rows

* (2) 地平線の行 (1 行)。**実線は引かない** (一番上に横棒を出さない)。
*     ここは黒で塗って位置を合わせるだけで、地面の始まり
*     は最初の帯が示す。塗り終えると U は床の先頭行を指す。
.sc_hz:
                lda     #1
                sta     w_cnt           * ★ 行数は D を作る前に決める
                ldb     w_hz
                lbsr    row_addr
                tfr     x,u
                ldd     #$0000
                tfr     d,x
                tfr     d,y
                lbsr    fill_rows
                stu     w_vram
                lda     #FRAME_H_LINES-1
                suba    w_hz
                lsra                    * 間引きぶん (= 論理行数) へ
                sta     w_rows

* (3) 床の帯を順に塗る
.sc_band:
                lda     w_nband
                lbeq    .sc_end
                deca
                sta     w_nband
                ldu     w_qsv
                ldb     ,u+             * cnt (この帯の行数)
                lda     ,u+             * pat (模様の番号)
                stu     w_qsv
                sta     w_tmp
                cmpb    w_rows          * 枠からはみ出さないようクリップ
                bls     .sc_ok
                ldb     w_rows
.sc_ok:
                tstb
                beq     .sc_band
                stb     w_cnt
                ldb     w_rows
                subb    w_cnt
                stb     w_rows
* 模様表から 6 byte を D/X/Y に載せる (= 帯 1 本につき 1 回だけ)
                ldb     w_tmp
                lda     #6
                mul                     * D = 番号 * 6
                addd    #flr_pat
                tfr     d,y
                ldd     0,y
                ldx     2,y
                ldy     4,y             * ★ Y は最後 (表の先頭を指すため)
                ldu     w_vram
                lbsr    fill_rows
                stu     w_vram
                bra     .sc_band
.sc_end:
                ldu     w_qsv
                lbra    qloop

* ============================================================
* fill_rect — w_rx/w_ry/w_rw/w_rh の矩形を A の値で塗る (描画先は w_base)
*   2 byte ずつ std で書く (= 1 byte ずつの 11 cycle/byte に対し 4 cycle/byte)。
*   幅が奇数なら最後の 1 byte だけ sta で足す。
*
*   ★★ std は A と B の**両方**を書き出す。つまりループカウンタを B に
*     置いたまま std すると、**1 byte おきにカウンタの値が VRAM へ書かれる**。
*     消し込みのつもりが $02 / $01 を撒き、自機や敵の跡が 1 dot 幅の白い
*     縦棒として残る、という形でしか症状が出ない (実際にこれを踏んだ)。
*     カウンタは必ずメモリに置き、D は「値:値」のまま触らないこと。
* ============================================================
fill_rect:
                sta     w_val           * 塗る値を憶える (D は後で作り直す)
                ldb     w_ry
                lda     #LINE_BYTES
                mul                     * D = y*80
                addb    w_rx
                adca    #0
                addd    w_base
                tfr     d,x             * X = VRAM
                lda     w_rh
                sta     w_cnt
.fr_line:
                pshs    x
                lda     w_rw
                lsra
                sta     w_pair          * ★ カウンタはメモリへ (B は使わない)
                beq     .fr_odd
                lda     w_val
                tfr     a,b             * D = 値:値
.fr_pair:
                std     ,x++
                dec     w_pair
                bne     .fr_pair
.fr_odd:
                lda     w_rw
                anda    #1
                beq     .fr_next
                lda     w_val
                sta     ,x
.fr_next:
                puls    x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .fr_line
                rts

* ============================================================
* erase_rect — w_rx/w_ry/w_rw/w_rh の矩形を黒で潰す (消し込み専用)
*   絵の幅は 2 / 3 / 4 byte しか無いので、幅ごとに展開して 1 行を
*   std 1〜2 発で潰す。汎用の fill_rect は 1 行あたり 80 cycle 超の
*   段取りが要り、小さな絵ほどその段取りが支配的になる (実測: 8x3 dot の
*   絵 8 個で 13fps 落ちた)。ここは 1 行 20〜25 cycle で済む。
*   幅がそれ以外なら fill_rect に任せる。
* ============================================================
erase_rect:
                ldb     w_ry
                lda     #LINE_BYTES
                mul                     * D = y*80
                addb    w_rx
                adca    #0
                addd    w_base
                tfr     d,x             * X = VRAM
                lda     w_rh
                sta     w_cnt
                ldb     w_rw
                cmpb    #2
                beq     .er2
                cmpb    #3
                beq     .er3
                cmpb    #4
                beq     .er4
                clra                    * その他の幅は汎用へ
                lbra    fill_rect
.er2:
                ldd     #0
.er2l:
                std     ,x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .er2l
                rts
.er3:
                ldd     #0
.er3l:
                std     ,x
                sta     2,x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .er3l
                rts
.er4:
                ldd     #0
.er4l:
                std     ,x
                std     2,x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .er4l
                rts

* ============================================================
* reg_rect — w_rx/w_ry/w_rw/w_rh を、いまの面の消し込み表へ積む
*   表が一杯なら黙って捨てる (= 絵が 1 フレーム余分に残るだけで暴走しない)
* ============================================================
reg_rect:
                lda     w_page
                bne     .rg1
                ldb     w_ln0
                cmpb    #LIST_MAX
                bhs     .rg_done
                incb
                stb     w_ln0
                decb
                lda     #4
                mul
                addd    #LIST0
                bra     .rg_put
.rg1:
                ldb     w_ln1
                cmpb    #LIST_MAX
                bhs     .rg_done
                incb
                stb     w_ln1
                decb
                lda     #4
                mul
                addd    #LIST1
.rg_put:
                tfr     d,x
                lda     w_rx
                sta     ,x+
                lda     w_ry
                sta     ,x+
                lda     w_rw
                sta     ,x+
                lda     w_rh
                sta     ,x+
.rg_done:
                rts

* ---- $07 PAGE p: 描画先の面を選び、その面を消し込む -----------------
cmd_page:
                ldb     ,u+
                stb     w_page
                ldx     #0
                tstb
                beq     .pg_set
                ldx     #$4000
.pg_set:
                stx     w_base
                stu     w_qsv
* この面に前回 (= 2 フレーム前) 描いた矩形を全部黒へ戻す
                ldb     w_page
                bne     .pg_l1
                ldb     w_ln0
                ldx     #LIST0
                clr     w_ln0
                bra     .pg_loop
.pg_l1:
                ldb     w_ln1
                ldx     #LIST1
                clr     w_ln1
.pg_loop:
                tstb
                beq     .pg_end
                stb     w_tmp
                lda     ,x+
                sta     w_rx
                lda     ,x+
                sta     w_ry
                lda     ,x+
                sta     w_rw
                lda     ,x+
                sta     w_rh
                pshs    x
                lbsr    erase_rect
                puls    x
                ldb     w_tmp
                decb
                bra     .pg_loop
.pg_end:
                ldu     w_qsv
                lbra    qloop

* ---- $05 SPRITE x,y,ph: 自機を貼る ---------------------------------
* 背景は常に黒なので OR で貼るだけ (= マスク不要)。
cmd_sprite:
                ldb     ,u+             * x (byte 単位 0-79)
                stb     w_rx
                ldb     ,u+             * y (line 0-199)
                stb     w_ry
                lda     #LINE_BYTES
                mul                     * D = y*80
                addb    w_rx
                adca    #0
                addd    w_base
                pshs    d               * VRAM アドレスを退避
                ldb     ,u+             * ph (位相 0-7)
                lda     #SPR_PHASE_BYTES
                mul                     * D = ph*80
                addd    #spr_data
                stu     w_qsv
                tfr     d,y             * Y = 絵の先頭
                puls    d
                tfr     d,x             * X = VRAM
                lda     #SPR_H_LINES
                sta     w_cnt
.sp_line:
                lda     ,x
                ora     ,y+
                sta     ,x+
                lda     ,x
                ora     ,y+
                sta     ,x+
                lda     ,x
                ora     ,y+
                sta     ,x+
                lda     ,x
                ora     ,y+
                sta     ,x+
                leax    LINE_BYTES-SPR_W_BYTES,x
                dec     w_cnt
                bne     .sp_line
                lda     #SPR_W_BYTES
                sta     w_rw
                lda     #SPR_H_LINES
                sta     w_rh
                lbsr    reg_rect
                ldu     w_qsv
                lbra    qloop

* ---- $0A WRITE ah,al,n,[byte]xn: サブ側の番地へ n byte 書く -----------
* 起動時の絵の転送 (爆発と敵弾の追加絵を VRAM 各面の末尾へ) と、文字描画
* (main 側のフォントを行ごとに運ぶ) の転送口。書くのはこのサブ CPU 自身で
* ある。ROM の TEST MOVE は VRAM を転送先に取れないので、この命令で運ぶ。
* VRAM ゲートは entry で開けてあるのでそのまま書ける。
cmd_write:
                ldx     ,u++            * X = 書込先
                ldb     ,u+             * n (1-105)
.pk_loop:
                lda     ,u+
                sta     ,x+
                decb
                bne     .pk_loop
                lbra    qloop

* ---- $09 OBJS n,[x,y,k|ph<<6]xn: まとめて貼る --------------------------
* k は絵の種類 (0-2 飛行体 / 3-5 球 / 6-8 低木 / 9-11 木 / 12-14 岩 /
* 15-17 自弾 / 18-20 爆発 A / 21-23 爆発 B / 24-26 敵楕円弾 / 27-29 敵 B。
* それぞれ 遠/中/近)。寸法と置場は enm_tab から引く。追加絵は VRAM
* 末尾にあるが、読み方は同じ。
cmd_objs:
                ldb     ,u+             * n
                stb     w_nobj
.ob_loop:
                tst     w_nobj
                lbeq    qloop
                dec     w_nobj
                ldb     ,u+             * x (byte)
                stb     w_rx
                ldb     ,u+             * y (line)
                stb     w_ry
                ldb     ,u+             * k | ph<<6
                tfr     b,a
                andb    #$3F            * B = k (0-63)
                lsra
                lsra
                lsra
                lsra
                lsra
                lsra                    * A = ph (0-3)
                lbsr    blit_obj
                bra     .ob_loop

* ============================================================
* blit_obj — 絵 k を位相 A で (w_rx, w_ry) に貼り、消し込み表へ積む
*   entry: B = k / A = ph / w_rx, w_ry / U は保存される
* ============================================================
blit_obj:
                pshs    a               * ph を退避
                lda     #6
                mul                     * D = k*6
                addd    #enm_tab
                tfr     d,x             * X = 寸法表の該当行
                puls    a               * A = ph
                ldb     3,x             * 位相ごとの byte 数 (下位。256 未満)
                mul                     * D = ph * 位相ごとの byte 数
                addd    4,x             * + 絵の先頭
                tfr     d,y             * Y = この位相の絵
                lda     ,x              * w
                sta     w_rw
                sta     w_ew
                lda     1,x             * h
                sta     w_rh
                sta     w_cnt
* VRAM = w_base + y*80 + x
                ldb     w_ry
                lda     #LINE_BYTES
                mul
                addb    w_rx
                adca    #0
                addd    w_base
                tfr     d,x             * X = VRAM
* 幅ごとに展開した貼り付けを選ぶ。2 byte ずつ ldd/std で運ぶ。
*
* 絵は **OR で貼る**。かつては上書きで貼っていた (read-modify-write が
* 要らず 1 byte 4 cycle 速い) が、絵の入れ物 (2/3/4 byte) には位相ずらし
* ぶんの黒い余白があり、横一列の編隊のように絵が重なると**後から貼った
* 絵の黒い余白が前の絵を欠けさせる** (遠の飛行体が並ぶと左半分が消えた)。
* キャラクタの面は背景が黒で床は別プレーンなので、OR なら重なりは白の
* 和になり欠けない。増分は 1 行あたり 2 byte で約 9 cycle
* (ldd ,x / ora ,y+ / orb ,y+ / std ,x = 22 cycle。上書きは 13 cycle)。
* ただし **幅 4 byte (近) だけは上書きのまま**にする。近の絵は 14 行 x
* 2 組で 1 個あたり約 270 cycle 増え、ボス戦 (近の節 7 個) の連射時で
* 55.9 → 52.7fps まで落ちた (幅 2/3 のみ OR なら 55.0fps)。近の絵は大きく
* 重なる時間が短く、上書きなら「手前の物が奥を隠す」ように見えるので
* 実害が小さい。速度優先で幅 2/3 のみ OR とした。
                ldb     w_ew
                cmpb    #2
                beq     .en2
                cmpb    #3
                beq     .en3
.en4:
                ldd     ,y++
                std     ,x
                ldd     ,y++
                std     2,x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .en4
                bra     .en_done
.en3:
                ldd     ,x
                ora     ,y+
                orb     ,y+
                std     ,x
                lda     2,x
                ora     ,y+
                sta     2,x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .en3
                bra     .en_done
.en2:
                ldd     ,x
                ora     ,y+
                orb     ,y+
                std     ,x
                leax    LINE_BYTES,x
                dec     w_cnt
                bne     .en2
.en_done:
                lbra    reg_rect        * 消し込み表へ積んで戻る

* ---- 床の模様表 (= scripts/make_floor.py が生成) --------------------
                include "../build/floor_data.s"

* ---- 自機と敵の絵 (= scripts/make_sprite.py が生成) -----------------
                include "../build/sprite_data.s"

                end
