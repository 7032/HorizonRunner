;
; ============================================================================
; FM-7 multi-load LOADM trampoline (6809 ASM, lwasm syntax)
;
; Companion source for d77_to_t77_chunks.py. **One** template serves every
; pass; what a pass actually does is decided entirely by a move table that
; the Python side appends after Stage 2. There is no per-shape variant to
; pick and no hand-maintained table of cases: the planner computes the moves
; from the binary size and emits them.
;
; Assembled with `lwasm --raw`, which does not pad across an `org` jump, so
; the output file is exactly:
;
;     [ Stage 1     : 26 B, assembled at $1400 ]
;     [ return stub :  6 B, assembled at $141A ]
;     [ Stage 2     :       assembled at $D000 ]
;
; and the Python side appends the move table right after Stage 2. The whole
; thing is loaded contiguously from $1400 by LOADM.
;
; Memory layout (assumed by every pass):
;   CLEAR ,&H13FF leaves $1400-$7FFF free; we use $1400-$5FFF.
;     $1400-$1419   Stage 1                       (always 26 B)
;     $141A-$141F   return stub                   (always  6 B; runs in place)
;     $1420-....    Stage 2 source + move table   (copied to $D000)
;     ....-$1FFF    zero padding
;     $2000-$5FFF   LOADM buffer                  (16 KiB; the chunk lands here)
;
; Stage 1 masks IRQ/FIRQ, switches the ROM overlay OFF (so $8000-$FBFF is
; plain RAM), copies Stage 2 **and the move table** up to $D000 — which is
; URA RAM and therefore survives the next pass overwriting $1400-$5FFF —
; and jumps into it.
;
; Stage 2 walks the move table and executes it. Each entry is:
;
;     $00 src(2) dst(2) limit(2)   forward copy; limit = source end (exclusive)
;     $01 src(2) dst(2) limit(2)   reverse copy; src/dst are the *ends*,
;                                  limit = source start
;     $FE entry(2)                 done: LDS #$FBFF, JMP entry   (final pass)
;     $FF                          done: jump to the return stub (intermediate)
;
; All 16-bit fields are big-endian (= what LDX ,U++ reads).
;
; The one sentinel the Python side patches is Stage 1's CMPX operand
; ($DEAD), which must end up as "$1420 + len(Stage 2) + len(move table)".
; Stage 2's own address ($D000) and the table's address ($D000 + len(Stage 2))
; are fixed at assembly time, so nothing else needs patching.
;
; Derived from the trampoline scheme of D77TOT77WAV (MIT). See
; D77TOT77WAV.LICENSE.txt.
; ============================================================================

STAGER_LOAD     equ     $1400
RETSTUB         equ     $141A           ; STAGER_LOAD + 26 (= Stage 1 size)
STAGE2_SRC_BASE equ     $1420           ; RETSTUB + 6 (= 復帰小片の大きさ)
STAGE2          equ     $D000
ENTRY_STACK     equ     $FBFF
ROM_PORT        equ     $FD0F

KIND_FWD        equ     $00
KIND_REV        equ     $01
KIND_JMP        equ     $FE
KIND_RTS        equ     $FF

PLACEHOLDER_END equ     $DEAD           ; <- patched: end of the copied source


; ---------------------------------------------------------------------------
; Stage 1 — runs at $1400 straight out of LOADM (auto-exec or EXEC &H1400).
; Exactly 26 bytes; STAGE2_SRC_BASE above depends on that.
; ---------------------------------------------------------------------------
                org     STAGER_LOAD
s1              orcc    #$50                    ; mask FIRQ/IRQ (the BASIC IRQ
                                                ;   handler lives in the ROM
                                                ;   overlay we are about to hide)
                lda     #$00
                sta     ROM_PORT                ; write -> overlay OFF
                ldx     #STAGE2_SRC_BASE
                ldy     #STAGE2
.l              lda     ,x+
                sta     ,y+
                cmpx    #PLACEHOLDER_END        ; <- patched by the Python side
                bne     .l
                jmp     STAGE2
s1_end


; ---------------------------------------------------------------------------
; Return stub — runs at $141A, i.e. in **low RAM**, and only on intermediate
; passes.
;
; This must not live with Stage 2. Stage 2 runs at $D000, which is *under*
; the ROM overlay: the instant the overlay comes back on, the next opcode is
; fetched from the ROM instead of from our copy, and the machine wanders off
; into the middle of BASIC. (The symptom is that `EXEC &H1400` never comes
; back to the `Ready` prompt — the load appears to hang after the first
; pass.) Low RAM is never overlaid, so switching the overlay back on here is
; safe.
;
; Nothing overwrites $141A before this runs: an intermediate pass only ever
; writes to chunk finals at $6000 and above and to the URA RAM stashes, and
; the final pass — which does write over $1400-$5FFF — ends with JMP instead
; of coming here. `simulate_plan` checks that for every pass.
; ---------------------------------------------------------------------------
                org     RETSTUB
retstub         lda     ROM_PORT                ; read -> overlay ON
                andcc   #$AF                    ; unmask FIRQ/IRQ
                rts                             ; -> back to F-BASIC
retstub_end


; ---------------------------------------------------------------------------
; Stage 2 — runs at $D000 (URA RAM). Interprets the move table.
; ---------------------------------------------------------------------------
                org     STAGE2
s2              ldu     #tbl
.next           ldb     ,u+                     ; kind
                cmpb    #KIND_JMP
                bhs     .fin                    ; $FE / $FF = terminator
                stb     kind
                ldx     ,u++                    ; source
                ldy     ,u++                    ; destination
                ldd     ,u++                    ; limit
                std     limit
                tst     kind
                bne     .rev
.fwd            lda     ,x+
                sta     ,y+
                cmpx    limit
                bne     .fwd
                bra     .next
.rev            lda     ,-x
                sta     ,-y
                cmpx    limit
                bne     .rev
                bra     .next

.fin            cmpb    #KIND_RTS
                bne     .jmpentry
                jmp     RETSTUB                 ; 低位 RAM でオーバレイを戻す
.jmpentry       ldx     ,u++                    ; entry address
                lds     #ENTRY_STACK
                jmp     ,x                      ; -> the game, ROM still OFF

kind            fcb     0                       ; scratch (RAM at $D000+)
limit           fdb     0                       ; scratch
tbl                                             ; move table is appended here
s2_end

                end
