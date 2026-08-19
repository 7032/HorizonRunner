# =============================================================
# HorizonRunner — FM-7 用 Makefile (CMOC + LWASM/LWLINK)
#
# プロジェクト名と本体開始アドレス、サブ CPU 側メモリマップは
# config.mk に集約。このファイルは触らなくて済むのが理想。
#
# 先行プロジェクトのビルド基盤から移植。font/sprite の画像パイプライン
# (TTF ダウンロード + Pillow) は本作では持たない (= ネットワーク
# アクセスに依存する処理を既定ビルドに残さない)。
# =============================================================

include config.mk

CMOC      = cmoc
LWASM     = lwasm
LWLINK    = lwlink

SCRIPTS      = ./scripts
SRC          = ./src
BUILD        = ./build

BIN          = $(BUILD)/$(NAME).bin
IPL          = $(BUILD)/ipl.bin
D77          = $(BUILD)/$(NAME).d77
HFE          = $(BUILD)/$(NAME).hfe      # HFE / 2D 機種用 (FM-7/FM77AV)
HFE2DD       = $(BUILD)/$(NAME)_2dd.hfe  # HFE / 2DD 機種用 (Double Step 相当 80trk)
T77          = $(BUILD)/$(NAME).t77      # カセットテープ (CMT) イメージ
WAV          = $(BUILD)/$(NAME).wav      # CMT ロード用 FSK 音声
CMTPROC      = $(BUILD)/$(NAME).cmt.txt  # CMT ロード操作手順テキスト
T77TOOL      = $(SCRIPTS)/d77_to_t77_chunks.py
T77_TRAMP_SRC = $(SCRIPTS)/trampoline.asm
T77_TRAMP    = $(BUILD)/trampoline.bin
BOOTROM      = $(BUILD)/bootrom.bin      # 自前ブート ROM (別ターゲット)
LINK_SCRIPT  = $(BUILD)/link.script      # config.mk の ORG から自動生成
LINK_MAP     = $(BUILD)/$(NAME).map      # リンクマップ (検証治具が番地を引く)

C_SRCS       = $(SRC)/c_main.c $(SRC)/c_subsys.c $(SRC)/c_subprog.c \
               $(SRC)/c_sound.c $(SRC)/c_text.c
ASM_SRCS     = $(SRC)/asm_crt0.s $(SRC)/asm_subsys.s $(SRC)/asm_runtime.s \
               $(SRC)/asm_test.s $(SRC)/asm_kbd.s $(SRC)/asm_timer.s \
               $(SRC)/asm_input.s

# ---- sub プログラム (= SUB_PROG_ADDR で動くサブ CPU 独自コード) ----
# org $C400 で raw bin にアセンブル → bin2asm.py で _subprog_bin[] と
# _subprog_len を持つ rodata に変換 → main 本体にリンク埋め込み。
# main 起動時に sub_takeover でサブへ転送し、sub_call で実行する。
SUBPROG_SRC     = $(SRC)/asm_subprog.s
# 自機の絵 (= ASCII アートから 8 位相ぶんの事前シフト済み
# ビットマップを焼く)。生成物なので build/ に置き、asm_subprog.s が
# include する。絵を直す時は scripts/make_sprite.py の ART を直す。
SPRITE_TOOL     = $(SCRIPTS)/make_sprite.py
SPRITE_DATA     = $(BUILD)/sprite_data.s
SPRITE_HDR      = $(BUILD)/sprite_geo.h
# 爆発の絵 (= サブ側のコード枠に入り切らないので、main が起動時に VRAM の
# 末尾へ WRITE コマンドで送らせる。中身と番地はこのヘッダに焼かれる)
EXPL_HDR        = $(BUILD)/expl_data.h
# 床の模様表と遠近表 (= 割り算をビルド時に済ませて実行時から追い出し、
# 模様の番号付けを asm と C で 1 箇所に閉じ込める)
# 敵の軌道表 (= 透視投影の割り算をビルド時に済ませる)
ENEMY_TOOL      = $(SCRIPTS)/make_enemy.py
ENEMY_HDR       = $(BUILD)/enemy_path.h
# 地上物 (低木 / 木) の軌道表 (= 床と同じ速度で迫るための滞在フレーム表)
GROUND_TOOL     = $(SCRIPTS)/make_ground.py
GROUND_HDR      = $(BUILD)/ground_path.h
# 1 面のスクリプト (= 何をいつ出すか。秒で書いた一覧をフレーム間隔の表に落とす)
STAGE_TOOL      = $(SCRIPTS)/make_stage.py
STAGE_HDR       = $(BUILD)/stage_script.h
# 8x8 の自前フォント (= タイトル / 得点 / 残機 / 幕。サブ側にはフォントを
# 置く余地が無いので、字形はメイン側に持ち WRITE コマンドでサブに書かせる)
FONT_TOOL       = $(SCRIPTS)/make_font.py
FONT_HDR        = $(BUILD)/font.h
FLOOR_TOOL      = $(SCRIPTS)/make_floor.py
FLOOR_DATA      = $(BUILD)/floor_data.s
FLOOR_HDR       = $(BUILD)/floor_table.h
SUBPROG_BIN     = $(BUILD)/subprog.bin
SUBPROG_DATA    = $(BUILD)/subprog_data.s

C_OBJS       = $(C_SRCS:$(SRC)/%.c=$(BUILD)/%.o)
ASM_OBJS     = $(ASM_SRCS:$(SRC)/%.s=$(BUILD)/%.o)
GEN_ASM_OBJS = $(BUILD)/subprog_data.o
OBJS         = $(ASM_OBJS) $(GEN_ASM_OBJS) $(C_OBJS)

.PHONY: all bin bootrom t77 wav hfe disk dist release help clean distclean

# ---- 配布物置き場 (git で追跡する。clean では消さない) ----
DIST         = ./disks
DIST_FILES   = $(DIST)/$(NAME).d77 $(DIST)/$(NAME).t77 \
               $(DIST)/$(NAME).wav $(DIST)/$(NAME).cmt.txt

# デフォルトターゲット: D77 + 自前ブート ROM + T77 / WAV、
# さらに配布イメージ一式を disks/ へコピーする。
# レイアウト機械検査 (scripts/check_layout.py) は本体ビルドの途中で必ず通る。
all: $(D77) $(BOOTROM) $(T77) $(WAV) dist

hfe: $(HFE) $(HFE2DD)

disk: $(D77) $(HFE) $(HFE2DD) $(BOOTROM)

release: $(D77) $(HFE) $(HFE2DD) $(BOOTROM) $(T77) $(WAV) dist

# disks/ への配布物コピー (D77 / T77 / WAV / テープ操作手順テキスト)
dist: $(DIST_FILES)

$(DIST)/$(NAME).d77: $(D77)
	@mkdir -p $(DIST)
	cp $< $@

$(DIST)/$(NAME).t77: $(T77)
	@mkdir -p $(DIST)
	cp $< $@

$(DIST)/$(NAME).wav: $(WAV)
	@mkdir -p $(DIST)
	cp $< $@

$(DIST)/$(NAME).cmt.txt: $(CMTPROC)
	@mkdir -p $(DIST)
	cp $< $@

help:
	@echo "make            : 本体 + D77 + ブート ROM + T77/WAV + disks/ へ配布物コピー"
	@echo "make hfe        : HFE 2 種 (実機フロッピーエミュレータ機器用)"
	@echo "make t77        : テープイメージ + WAV + 操作手順テキスト"
	@echo "make disk       : ディスク用ぜんぶ (D77 + HFE 2 種 + ブート ROM)"
	@echo "make dist       : 配布物 (D77/T77/WAV/手順 TXT) を disks/ へコピー"
	@echo "make release    : 全部作る"
	@echo "make bin        : 本体 BIN だけ"
	@echo "make bootrom    : 自前ブート ROM だけ"
	@echo "make clean      : build/ を削除 (disks/ の配布物は消さない)"

bin: $(BIN)

bootrom: $(BOOTROM)

build:
	mkdir -p build

# C → オブジェクト
#   ヘッダ全部を依存に列挙 (= header 変更時の再 compile 漏れ防止)。
C_HEADERS = $(wildcard $(SRC)/*.h)

$(BUILD)/c_main.o: $(FLOOR_HDR) $(ENEMY_HDR) $(GROUND_HDR) $(SPRITE_HDR) $(STAGE_HDR) $(EXPL_HDR)
$(BUILD)/c_text.o: $(FONT_HDR)

$(BUILD)/%.o: $(SRC)/%.c $(C_HEADERS) | build
	$(CMOC) -c --intermediate --intdir=$(BUILD) -O2 -o $@ $<

# ASM (オブジェクト形式) → オブジェクト
$(BUILD)/%.o: $(SRC)/%.s | build
	$(LWASM) --obj -o $@ $<

# 生成 ASM (build/*.s) → オブジェクト
$(BUILD)/subprog_data.o: $(SUBPROG_DATA) | build
	$(LWASM) --obj -o $@ $<

# lwlink 用スクリプト生成 (config.mk の ORG を反映)
#   全セクション ($(ORG) 起点で連続) を明示し、CMOC が生成する
#   rodata / start / initgl_* が他アドレスで解決される事故を防ぐ。
$(LINK_SCRIPT): config.mk Makefile | build
	@printf 'section code   load %s\nsection rodata\nsection initgl_start\nsection initgl\nsection initgl_end\nsection start\nsection program_end\nsection rwdata\nsection bss\n' '$(ORG)' > $@

# リンクして本体 BIN
#   --map も必ず同時に出す。変数の番地はビルドのたびに動くので、検証時は
#   番地を直書きせずこのマップから引くこと。
$(BIN): $(OBJS) $(LINK_SCRIPT)
	$(LWLINK) --raw --script=$(LINK_SCRIPT) --map=$(LINK_MAP) --output=$@ $(OBJS)

# IPL は単独で raw アセンブル (BODY_LOAD = config.mk の ORG を -D で渡す)
$(IPL): $(SRC)/asm_ipl.s config.mk | build
	$(LWASM) --raw -D BODY_LOAD=$(ORG) -o $@ $<

# ---- sub プログラム ----
# アセンブル直後に scripts/check_layout.py を必ず通す。これは
#   (a) subprog.bin がスタック予約領域 (SUB_CODE_END) を侵食していないか
#   (b) config.mk / asm_subprog.s / c_subprog.h のアドレス定数が一致するか
# を検査し、違反ならビルドを失敗させる。
$(SPRITE_DATA) $(SPRITE_HDR) $(EXPL_HDR) &: $(SPRITE_TOOL) config.mk | build
	python3 $(SPRITE_TOOL) $(SPRITE_DATA) $(SPRITE_HDR) $(EXPL_HDR)

$(ENEMY_HDR): $(ENEMY_TOOL) | build
	python3 $(ENEMY_TOOL) $@

$(GROUND_HDR): $(GROUND_TOOL) $(FLOOR_TOOL) | build
	python3 $(GROUND_TOOL) $@

$(STAGE_HDR): $(STAGE_TOOL) $(ENEMY_TOOL) $(GROUND_TOOL) | build
	python3 $(STAGE_TOOL) $@

$(FONT_HDR): $(FONT_TOOL) | build
	python3 $(FONT_TOOL) $@

$(FLOOR_DATA) $(FLOOR_HDR) &: $(FLOOR_TOOL) | build
	python3 $(FLOOR_TOOL) $(FLOOR_DATA) $(FLOOR_HDR)

$(SUBPROG_BIN): $(SUBPROG_SRC) $(SPRITE_DATA) $(FLOOR_DATA) $(SRC)/c_subprog.h config.mk \
                $(SCRIPTS)/check_layout.py | build
	$(LWASM) --raw -o $@ $<
	@python3 $(SCRIPTS)/check_layout.py $@ || (rm -f $@; exit 1)

$(SUBPROG_DATA): $(SUBPROG_BIN) $(SCRIPTS)/bin2asm.py
	python3 $(SCRIPTS)/bin2asm.py $(SUBPROG_BIN) $@ subprog

# 自前ブート ROM ($FE00-$FFFF, 512 byte)
$(BOOTROM): $(SRC)/asm_bootrom.s | build
	$(LWASM) --raw -o $@.raw $<
	python3 $(SCRIPTS)/pad_bootrom.py $@.raw $@

# D77 は IPL と本体 BIN を結合
$(D77): $(IPL) $(BIN)
	python3 $(SCRIPTS)/bin2d77.py \
	    --ipl $(IPL) \
	    --body $(BIN) \
	    --name $(NAME) \
	    --org $(ORG) \
	    -o $@

# HFE は D77 を IBM System 34 互換 MFM へ変換
$(HFE): $(D77) $(SCRIPTS)/d77_to_hfe.py
	python3 $(SCRIPTS)/d77_to_hfe.py $(D77) --mode 2d -o $@

$(HFE2DD): $(D77) $(SCRIPTS)/d77_to_hfe.py
	python3 $(SCRIPTS)/d77_to_hfe.py $(D77) --mode 2dd -o $@

# ---- T77 / WAV (CMT カセットテープ、要る時だけ) ----
t77: $(T77)

wav: $(WAV)

$(T77_TRAMP): $(T77_TRAMP_SRC) | build
	$(LWASM) --raw -o $@ $<

$(T77): $(D77) $(T77TOOL) $(T77_TRAMP)
	python3 $(T77TOOL) $(D77) --addr $(ORG) --tramp $(T77_TRAMP) \
	    -o $(T77) -w $(WAV) -t $(CMTPROC)

$(WAV): $(T77)
	@:

$(CMTPROC): $(T77)
	@:

# build/ の rm は WSL+Windows FS で permission denied が起きることが
# あるので `-` で続行可能にしておく (= 本質エラーではない)。
clean:
	-rm -rf build

distclean: clean
