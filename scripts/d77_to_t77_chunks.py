#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# このスクリプトは MIT ライセンスのツール D77TOT77WAV (Copyright (c) 2026
# Naomitsu.Tsugiiwa) を本テンプレートに取り込んだものです。ライセンス全文は
# 同ディレクトリの D77TOT77WAV.LICENSE.txt を参照。
"""
Generic D77 -> T77 converter for FM-7 F-BASIC.

Pipeline:
    D77 -> raw sector concatenation -> BIN
        -> split into 16 KiB chunks and work out, *mechanically*, how each
           chunk reaches its final memory location
        -> each pass is one LOADM file; concatenate as a T77 tape image
           and emit a TXT operator procedure

Memory layout used by every pass:
    CLEAR ,&H13FF leaves $1400-$7FFF free for us.
    $1400-$1419   Stage 1   (26 bytes, fixed)
    $141A-....    Stage 2 source + move table (copied to $D000)
    ....-$1FFF    zero padding
    $2000-$5FFF   LOADM buffer (16 KiB)

How the split is decided (no hand-maintained table of cases)
------------------------------------------------------------
Every pass overwrites exactly one range: $1400-$5FFF (the LOADM block).
Call it the *volatile* range. Everything else that has already been
placed survives. From that single fact the plan falls out:

  * chunk 0 is always loaded LAST (via LOADM ",,R"), because its final
    position is the lowest and therefore the one most likely to sit under
    the volatile range;
  * every other chunk is loaded before it, highest memory first. The part
    of such a chunk that lands OUTSIDE the volatile range is written
    straight to its final position; the part that would land INSIDE it is
    parked in URA RAM ($8000-$EFFF, plain RAM once the ROM overlay is off)
    and moved back by the final pass, after the final pass has emptied the
    buffer.

So the boundaries are a function of (entry address, binary size) alone.
Growing the binary moves them automatically; nothing here needs editing.

The whole plan is then *simulated byte for byte* (`simulate_plan`) before
anything is written: the simulator replays every LOADM block and every
trampoline move over a 64 KiB memory image and insists that the result is
the original binary, sitting at the entry address, with nothing having
trampled the trampoline, the URA RAM stashes or the stack reservation.
A plan that would break is a build failure, not a silent corruption.

One trampoline template (scripts/trampoline.asm, assembled by the Makefile)
serves every pass; the move table appended to it says what that pass does.

Usage:
    python3 d77_to_t77_chunks.py game.d77 --addr 0x0200 \\
        --tramp build/trampoline.bin \\
        [--skip N] [--size N] [--out game.t77] [--txt game.txt]
"""

import argparse
import os
import struct
import sys


# ===== FM-7 memory layout =====
#
# ここの値は config.mk の TAPE_* が正であり、 make のたびに
# scripts/check_layout.py が config.mk / trampoline.asm と突き合わせる。
# 片方だけ直すと 「転送先だけがずれる」 という静かな壊れ方をするため。

CLEAR_VALUE       = 0x13FF
STAGER_LOAD_ADDR  = 0x1400
BUFFER_ADDR       = 0x2000
CHUNK_SIZE        = 0x4000
BUFFER_END        = BUFFER_ADDR + CHUNK_SIZE        # $6000
STAGE2_ADDR       = 0xD000
ENTRY_STACK       = 0xFBFF

# LOADM ブロックの先頭に居る 「その場で走る」 部分の大きさ。
#   Stage 1 (26 byte) + 復帰小片 (6 byte)
# 復帰小片は ROM オーバレイを戻して F-BASIC へ帰るだけの 3 命令だが、
# **低位 RAM で走らせなければならない**。 Stage 2 が居る $D000 は
# オーバレイの下なので、 そこでオーバレイを戻すと次の命令が ROM から
# 取られて BASIC の中へ迷い込む (= 中間パスが `Ready` に戻らず止まる)。
HEAD_SIZE         = 26 + 6
RETSTUB_ADDR      = STAGER_LOAD_ADDR + 26           # $141A
STAGE2_SRC_BASE   = STAGER_LOAD_ADDR + HEAD_SIZE    # $1420

# 毎パス必ず上書きされる範囲 (= LOADM ブロックが着地する範囲)。
# 分割の境目はこの 1 本の区間から機械的に導かれる。
VOLATILE_BEG      = STAGER_LOAD_ADDR
VOLATILE_END      = BUFFER_END                      # $1400-$5FFF

# 裏 RAM の退避プール。 ROM オーバレイを OFF にすると素の RAM になる領域の
# うち、 $F000 以上はスタックのために空けておく。
STASH_LO          = 0x8000
STASH_HI          = 0xF000

# 常駐できる上限 = 最終パスが張り直すスタックの底の 1 つ上。
RESIDENT_END      = ENTRY_STACK + 1                 # $FC00

# トランポリン (Stage 2 + 移動表) のために $D000 から確保する枠。
# 実測が枠を超えたら生成を失敗させる。
TRAMP_RESERVE     = 0x0200

LOADM_LO_LIMIT    = 0x0800

# 移動表のオペコード (= scripts/trampoline.asm の KIND_* と一致させる)
MOVE_FWD          = 0x00
MOVE_REV          = 0x01
END_JMP           = 0xFE
END_RTS           = 0xFF


# ===== D77 disk image parsing =====

_D77_FILL_BYTES = (0x00, 0xE5, 0xFF)


def _sector_is_fill(data):
    """A 'fill' sector contains a single repeated byte from the known FM-7
    format fillers ($00 / $E5 / $FF). These are what the disk formatter
    leaves on every sector not yet written by a real file."""
    if not data:
        return True
    s = set(data)
    return len(s) == 1 and next(iter(s)) in _D77_FILL_BYTES


def extract_d77_payload(d77_data, trim_trailing_fill=True, verbose=False):
    """Walk the D77 track-offset table and concatenate sector data in CHR
    (cylinder, head, record) order.

    If `trim_trailing_fill` is True (default), drop trailing tracks whose
    every sector is a known fill byte ($00 / $E5 / $FF) — those are the
    parts of the floppy that the formatter wrote and no file has touched.
    Fill sectors WITHIN a track that also contains real data are kept
    intact: programs sometimes pad their data area with $E5 / $00 and the
    IPL still loads those sectors as part of the program image.

    Pass `trim_trailing_fill=False` to disable the heuristic and get the
    raw concatenation of every sector (useful when the caller wants to
    control the exact byte range via `--size`).
    """
    sectors = []
    for ti in range(164):
        off = struct.unpack('<I', d77_data[0x20 + ti * 4:0x24 + ti * 4])[0]
        if off == 0 or off >= len(d77_data):
            continue
        sec_count = struct.unpack('<H', d77_data[off + 4:off + 6])[0]
        pos = off
        for _ in range(sec_count):
            if pos + 16 > len(d77_data):
                break
            c = d77_data[pos]
            h = d77_data[pos + 1]
            r = d77_data[pos + 2]
            sz = struct.unpack('<H', d77_data[pos + 14:pos + 16])[0]
            sec_data = d77_data[pos + 16:pos + 16 + sz]
            sectors.append((c, h, r, sec_data))
            pos += 16 + sz
    sectors.sort(key=lambda s: (s[0], s[1], s[2]))

    if not trim_trailing_fill:
        payload = b''.join(s[3] for s in sectors)
        if verbose:
            print(f'    D77 raw extract   : {len(sectors)} sectors, '
                  f'{len(payload)} bytes (no fill-track trim)',
                  file=sys.stderr)
        return payload

    # Index, for each track, whether it has any non-fill content.
    track_has_data = {}
    for c, h, _, sec_data in sectors:
        if not _sector_is_fill(sec_data):
            track_has_data[(c, h)] = True
        elif (c, h) not in track_has_data:
            track_has_data.setdefault((c, h), False)

    last_used = -1
    for i, (c, h, _, _) in enumerate(sectors):
        if track_has_data.get((c, h), False):
            last_used = i

    if last_used < 0:
        if verbose:
            print('warn: D77 has no non-fill tracks; payload is empty',
                  file=sys.stderr)
        return b''

    used = sectors[:last_used + 1]
    payload = b''.join(s[3] for s in used)
    if verbose:
        total = len(sectors)
        kept = len(used)
        c, h, r, _ = used[-1]
        used_tracks = sum(1 for v in track_has_data.values() if v)
        total_tracks = len(track_has_data)
        print(f'    D77 used tracks    : {used_tracks} of {total_tracks}',
              file=sys.stderr)
        print(f'    D77 used sectors   : {kept} of {total}  '
              f'(last data-bearing track ends at C{c} H{h} R{r})',
              file=sys.stderr)
        print(f'    D77 used bytes     : {len(payload)}  '
              f'(dropped {total - kept} sectors of trailing fill)',
              file=sys.stderr)
    return payload


# ===== Trampoline template loading and patching =====

def _u16_be(v):
    return bytes([(v >> 8) & 0xFF, v & 0xFF])


def _patch_once(buf, sentinel, value):
    """Replace exactly one occurrence of `sentinel` (2 bytes) with `value`
    (16-bit, written big-endian). Raises if missing or duplicated."""
    i = buf.find(sentinel)
    if i < 0:
        raise RuntimeError(
            f"sentinel {sentinel.hex().upper()} not found in template")
    j = buf.find(sentinel, i + 2)
    if j >= 0:
        raise RuntimeError(
            f"sentinel {sentinel.hex().upper()} appears twice — ambiguous patch")
    if not (0 <= value <= 0xFFFF):
        raise RuntimeError(f"patch value ${value:X} out of 16-bit range")
    buf[i:i + 2] = _u16_be(value)


def encode_move(src, dst, length):
    """1 手ぶんの移動表エントリを組む。

    向きは 「重なっても壊さない方」 を機械的に選ぶ:
      dst < src なら前方 (低位から)、 dst > src なら後方 (高位から)。
    dst == src は何もしなくてよいので 0 byte を返す。
    """
    if length <= 0:
        raise RuntimeError(f'移動長が 0 以下: ${src:04X}->${dst:04X} {length}')
    if src + length > 0x10000 or dst + length > 0x10000:
        raise RuntimeError(
            f'移動が $FFFF を跨ぐ: ${src:04X}->${dst:04X} {length} byte')
    if dst == src:
        return b''
    if dst < src:
        return (bytes([MOVE_FWD]) + _u16_be(src) + _u16_be(dst)
                + _u16_be(src + length))
    return (bytes([MOVE_REV]) + _u16_be(src + length) + _u16_be(dst + length)
            + _u16_be(src))


def build_trampoline(template, moves, entry):
    """テンプレート (Stage 1 + 復帰小片 + Stage 2) に移動表を付けて 1 パス
    ぶんを組む。

    `entry` が None なら中間パス (復帰小片へ跳んで F-BASIC へ帰る)、
    アドレスなら最終パス (LDS してエントリへ JMP)。 Stage 1 の CMPX には
    「Stage 2 + 移動表の終端」 を書き込む。 これが唯一のパッチ箇所である。
    """
    buf = bytearray(template)
    table = bytearray()
    for src, dst, length in moves:
        table += encode_move(src, dst, length)
    if entry is None:
        table.append(END_RTS)
    else:
        table.append(END_JMP)
        table += _u16_be(entry)
    stage2_len = len(template) - HEAD_SIZE
    if stage2_len <= 0:
        raise RuntimeError('トランポリンのテンプレートが短すぎる')
    _patch_once(buf, b'\xDE\xAD', STAGE2_SRC_BASE + stage2_len + len(table))
    run_len = stage2_len + len(table)
    if run_len > TRAMP_RESERVE:
        raise RuntimeError(
            f'トランポリンが $D000 の確保枠 {TRAMP_RESERVE} byte を超えた '
            f'({run_len} byte)')
    return bytes(buf) + bytes(table), run_len


def wrap_block(trampoline, chunk_data):
    """Pack a patched trampoline + 16 KiB chunk into a single LOADM block
    that loads contiguously from STAGER_LOAD_ADDR through BUFFER_END-1."""
    assert len(chunk_data) == CHUNK_SIZE
    pad = BUFFER_ADDR - (STAGER_LOAD_ADDR + len(trampoline))
    if pad < 0:
        raise RuntimeError(
            f"trampoline overflows the buffer base: {len(trampoline)} B does "
            f"not fit between ${STAGER_LOAD_ADDR:04X} and ${BUFFER_ADDR:04X}")
    return trampoline + bytes(pad) + chunk_data


# ===== LOADM payload framing and T77 encoding =====

def make_loadm_payload(block_addr, block, exec_addr):
    out = bytearray()
    out += struct.pack('>BHH', 0x00, len(block), block_addr)
    out += block
    out += struct.pack('>BHH', 0xFF, 0x0000, exec_addr)
    return bytes(out)


# Tape FSK half-cycle durations in T77 ticks (1 tick = 16 CPU cycles =
# 8.92 us at 1.794 MHz). MARK_HALF = 50 yields a full mark cycle of
# ~893 us (~1120 Hz), matching the FM-7 leader tone on real hardware.
MARK_HALF = 50
SPACE_HALF = 0x1A
POL = 0x8000
LEADER_BYTES = 256
GAP_LEADER_BYTES = 40
DATA_PAYLOAD_SIZE = 255
WAV_SILENCE_MARKER = 0x0000


def _uart_bits(b):
    bits = [0]
    for i in range(8):
        bits.append((b >> i) & 1)
    bits.extend([1, 1])
    return bits


def _bits_to_halfcycles(bits):
    out = []
    for b in bits:
        dur = MARK_HALF if b else SPACE_HALF
        out.append(dur | POL)
        out.append(dur)
    return out


def _encode_bytes(bs):
    bits = []
    for b in bs:
        bits.extend(_uart_bits(b))
    return _bits_to_halfcycles(bits)


def _leader(n):
    return _encode_bytes(bytes([0xFF] * n))


def _chksum(b):
    return sum(b) & 0xFF


def _tape_header_block(name, attr=0x02):
    sync = bytearray([0x01, 0x3C])
    content = bytearray([0x00, 0x14])
    fn = name.upper()[:8].encode('ascii').ljust(8, b' ')
    content += fn
    content.append(attr)
    content += bytes(11)
    content.append(_chksum(content))
    return bytes(sync + content)


def _tape_data_block(payload):
    sync = bytearray([0x01, 0x3C])
    content = bytearray([0x01, 0xFF])
    chunk = bytearray(payload)
    if len(chunk) < DATA_PAYLOAD_SIZE:
        chunk += bytes(DATA_PAYLOAD_SIZE - len(chunk))
    content += chunk
    content.append(_chksum(content))
    return bytes(sync + content)


def _tape_end_block():
    sync = bytearray([0x01, 0x3C])
    content = bytearray([0xFF])
    content.append(_chksum(content))
    return bytes(sync + content)


def _build_one_tape_file(loadm_bytes, name):
    hc = []
    hc += _leader(LEADER_BYTES)
    hc += _encode_bytes(_tape_header_block(name, attr=0x02))
    pos = 0
    while pos < len(loadm_bytes):
        chunk = loadm_bytes[pos:pos + DATA_PAYLOAD_SIZE]
        hc += _leader(GAP_LEADER_BYTES)
        hc += _encode_bytes(_tape_data_block(chunk))
        pos += DATA_PAYLOAD_SIZE
    hc += _leader(GAP_LEADER_BYTES)
    hc += _encode_bytes(_tape_end_block())
    return hc


# ---------------------------------------------------------------------------
# ASCII 保存の BASIC ファイル (= テープの先頭に置くローダー)
#
# 書式は F-BASIC 自身に SAVE "CAS0:<名前>",A を実行させ、 出てきた波形を
# 復号して実測したものである (Issue #40)。 ブロックの 2 byte 目は **実長**で、
# チェックサムは 識別バイト + 長さバイト + 本文 の総和 & 0xFF。
#
#   ヘッダ : 01 3C | 00 14 | 名前 8 | 属性 00 | FF FF | 00 x9 | チェックサム
#   データ : 01 3C | 01 <実長> | 本文 | チェックサム
#   終端   : 01 3C | FF 00 | チェックサム
#
# 本文は 先頭に CR、 各行の後ろにも CR を置く (末尾に $1A は付けない)。
# ブロック間のリーダも実測に合わせて 256 byte 取る (機械語ファイルの 40 byte
# より長い)。
# ---------------------------------------------------------------------------
BASIC_ATTR       = 0x00     # 属性: BASIC プログラム
BASIC_ASCII_FLAG = 0xFF     # ASCII 保存の識別
BASIC_GAP_LEADER = 256      # ブロック間リーダ (実測値)
LOADER_NAME      = 'LOADER' # テープ先頭の BASIC ローダーのファイル名


def _basic_header_block(name):
    content = bytearray([0x00, 0x14])
    content += name.upper()[:8].encode('ascii').ljust(8, b' ')
    content += bytes([BASIC_ATTR, BASIC_ASCII_FLAG, BASIC_ASCII_FLAG]) + bytes(9)
    content.append(_chksum(content))
    return bytes(bytearray([0x01, 0x3C]) + content)


def _basic_data_block(payload):
    content = bytearray([0x01, len(payload)]) + bytearray(payload)
    content.append(_chksum(content))
    return bytes(bytearray([0x01, 0x3C]) + content)


def _basic_end_block():
    content = bytearray([0xFF, 0x00])
    content.append(_chksum(content))
    return bytes(bytearray([0x01, 0x3C]) + content)


def _pass_seconds(info):
    """1 パスをテープに流すのに掛かる実時間 (= 波形そのものの長さ)。"""
    # 半サイクルの上位 bit は極性フラグなので、 長さだけを取り出して足す。
    return (sum(h & ~POL for h in _build_one_tape_file(info['loadm'], info['name']))
            * TICK_SCALE_CY / CPU_CLOCK_HZ)


def basic_loader_lines(n_passes, total_secs=None):
    """パス計画から BASIC ローダーの行を機械的に組む。 手で書き換える表は無い。

    中身は **手で打つ手順と全く同じ並び**にしてある。 中間パスを
    `LOADM "CAS0:",,R` の auto-exec で済ませる形も実測では通るが (復帰小片の
    RTS がそのまま F-BASIC へ戻る。 Issue #40)、 手打ち手順のほうは実機で
    確認済みの並びなので、 未知を 「BASIC プログラムから実行できるか」 の
    1 点だけに絞る。
    """
    lines = [f'10 CLEAR ,&H{CLEAR_VALUE:04X}']
    no = 20
    # プログラムから呼んだ LOADM は Searching / Found: を出さない (実測)。
    # 案内を出しておかないと、 読み込みの間じゅう画面が無反応に見える。
    if total_secs:
        lines.append(f'{no} PRINT "LOADING - PLEASE WAIT ABOUT '
                     f'{int(total_secs / 60 + 0.5)} MIN"')
        no += 10
    for i in range(n_passes):
        if i == n_passes - 1:
            lines.append(f'{no} LOADM "CAS0:",,R')
            no += 10
        else:
            lines.append(f'{no} LOADM "CAS0:"')
            lines.append(f'{no + 10} EXEC &H{STAGER_LOAD_ADDR:04X}')
            no += 20
    return lines


def _build_one_basic_file(lines, name):
    text = b'\r' + b'\r'.join(l.encode('ascii') for l in lines) + b'\r'
    hc = []
    hc += _leader(BASIC_GAP_LEADER)
    hc += _encode_bytes(_basic_header_block(name))
    pos = 0
    while pos < len(text):
        hc += _leader(BASIC_GAP_LEADER)
        hc += _encode_bytes(_basic_data_block(text[pos:pos + DATA_PAYLOAD_SIZE]))
        pos += DATA_PAYLOAD_SIZE
    hc += _leader(BASIC_GAP_LEADER)
    hc += _encode_bytes(_basic_end_block())
    return hc


def _build_file(entry):
    """entry = (name, kind, payload)。 kind は 'loadm' か 'basic'。"""
    name, kind, payload = entry
    if kind == 'basic':
        return _build_one_basic_file(payload, name)
    return _build_one_tape_file(payload, name)


def build_t77(files, mark_inter_file_silence=True):
    hc = []
    for i, entry in enumerate(files):
        if i > 0 and mark_inter_file_silence:
            hc.append(WAV_SILENCE_MARKER)
        hc += _build_file(entry)
        hc += _leader(64)
    hc += _leader(32)
    # T77 tape image format magic header (18 bytes, fixed).
    out = bytearray(bytes.fromhex(
        '58 4D 37 20 54 41 50 45 20 49 4D 41 47 45 20 30 00 00'.replace(' ', '')))
    for v in hc:
        out += struct.pack('>H', v)
    return bytes(out)


# ===== WAV synthesis (44.1 kHz / 16-bit signed / mono) =====
#
# Each T77 half-cycle entry has a duration (in 16-cycle ticks at the 1.794
# MHz CPU clock) and a polarity flag in bit 15. We render that as 16-bit
# signed PCM: silence = 0, the polarity-high half-cycle = +amplitude, the
# polarity-low half-cycle = -amplitude. A fractional accumulator keeps the
# emitted sample count aligned across half-cycles that are not an integer
# number of samples long.
#
# Real tape recordings show a slight roll-off at every level transition
# (a few samples of intermediate amplitude where the tape head and AC
# coupling can't quite snap from -peak to +peak). We approximate that
# with a short cosine ramp at every polarity change.

import math

WAV_SAMPLE_RATE   = 44100
WAV_AMPLITUDE     = 24000      # ~73 % of full-scale, leaves headroom
WAV_SILENCE_LVL   = 0
WAV_HIGH_LVL      =  WAV_AMPLITUDE
WAV_LOW_LVL       = -WAV_AMPLITUDE
WAV_RAMP_SAMPLES  = 3          # cos-shaped transition width

CPU_CLOCK_HZ      = 1_794_000
TICK_SCALE_CY     = 16
WAV_SAMPLES_PER_TICK = WAV_SAMPLE_RATE * TICK_SCALE_CY / CPU_CLOCK_HZ


def _cos_ramp(src, dst, n):
    """Return n samples ramping from `src` toward `dst` along a raised
    cosine. The intermediate sample lands at the midpoint (DC center for
    a -peak-to-+peak swing), matching the way an analog tape recording
    shows one near-zero sample flanked by two part-amplitude samples on
    either side of each polarity flip."""
    out = []
    for k in range(n):
        t = (k + 1) / (n + 1)
        shape = (1 - math.cos(math.pi * t)) / 2
        out.append(int(round(src + (dst - src) * shape)))
    return out


_RAMP_S_TO_H = struct.pack(f'<{WAV_RAMP_SAMPLES}h',
                           *_cos_ramp(WAV_SILENCE_LVL, WAV_HIGH_LVL, WAV_RAMP_SAMPLES))
_RAMP_S_TO_L = struct.pack(f'<{WAV_RAMP_SAMPLES}h',
                           *_cos_ramp(WAV_SILENCE_LVL, WAV_LOW_LVL,  WAV_RAMP_SAMPLES))
_RAMP_H_TO_L = struct.pack(f'<{WAV_RAMP_SAMPLES}h',
                           *_cos_ramp(WAV_HIGH_LVL,    WAV_LOW_LVL,  WAV_RAMP_SAMPLES))
_RAMP_L_TO_H = struct.pack(f'<{WAV_RAMP_SAMPLES}h',
                           *_cos_ramp(WAV_LOW_LVL,     WAV_HIGH_LVL, WAV_RAMP_SAMPLES))
_BYTES_HIGH  = struct.pack('<h', WAV_HIGH_LVL)
_BYTES_LOW   = struct.pack('<h', WAV_LOW_LVL)
_BYTES_SIL   = struct.pack('<h', WAV_SILENCE_LVL)


def _i16le_run(value_bytes, count):
    """Repeat a 2-byte sample word `count` times."""
    return value_bytes * count


def _silence_samples(seconds):
    n = int(round(seconds * WAV_SAMPLE_RATE))
    return _i16le_run(_BYTES_SIL, n)


def _halfcycles_to_samples(hc):
    """Render a list of T77 half-cycle entries to 16-bit signed PCM, with
    a short cosine ramp at every polarity change to soften the otherwise
    perfectly-square edges."""
    out = bytearray()
    acc = 0.0
    prev_pol = None        # None = coming from silence
    for entry in hc:
        dur = entry & 0x7FFF
        if dur == 0:
            continue                          # silence cue, skipped
        pol = (entry & 0x8000) != 0
        acc += dur * WAV_SAMPLES_PER_TICK
        n = int(acc)
        acc -= n
        if n <= 0:
            continue
        if prev_pol is None:
            ramp = _RAMP_S_TO_H if pol else _RAMP_S_TO_L
        elif prev_pol == pol:
            ramp = b''
        else:
            ramp = _RAMP_L_TO_H if pol else _RAMP_H_TO_L
        body_bytes = _BYTES_HIGH if pol else _BYTES_LOW
        ramp_n = len(ramp) // 2
        if ramp_n and n > ramp_n:
            out += ramp
            out += _i16le_run(body_bytes, n - ramp_n)
        else:
            out += _i16le_run(body_bytes, n)
        prev_pol = pol
    return bytes(out)


def _wrap_wav(samples):
    """Build a canonical RIFF/WAVE container around the raw PCM bytes."""
    n = len(samples)
    bits_per_sample = 16
    channels = 1
    byte_rate = WAV_SAMPLE_RATE * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    fmt = struct.pack('<IHHIIHH',
                      16,                # fmt chunk size
                      1,                 # PCM
                      channels,
                      WAV_SAMPLE_RATE,
                      byte_rate,
                      block_align,
                      bits_per_sample)
    return (b'RIFF' + struct.pack('<I', 36 + n) + b'WAVE'
            + b'fmt ' + fmt
            + b'data' + struct.pack('<I', n) + samples)


def build_wav(files, head_silence=5.0, gap_silence=5.0, tail_silence=5.0):
    """Render each LOADM file's tape stream to PCM samples with DC-center
    silences inserted at the head, between files, and at the tail."""
    samples = bytearray()
    samples += _silence_samples(head_silence)
    for i, entry in enumerate(files):
        if i > 0:
            samples += _silence_samples(gap_silence)
        samples += _halfcycles_to_samples(_build_file(entry))
    samples += _silence_samples(tail_silence)
    return _wrap_wav(bytes(samples))


# ===== Placement puzzle: deciding the pass plan =====

def split_into_chunks(binary):
    chunks = []
    pos = 0
    while pos < len(binary):
        ch = binary[pos:pos + CHUNK_SIZE]
        if len(ch) < CHUNK_SIZE:
            ch = ch + bytes(CHUNK_SIZE - len(ch))
        chunks.append(ch)
        pos += CHUNK_SIZE
    return chunks


def trim_trailing_zeros(data):
    """Drop trailing zero bytes (typical of empty D77 sectors past the data
    region). Returns the trimmed bytes — at least 1 byte if any non-zero
    exists; empty bytes if `data` is all zero."""
    end = len(data)
    while end > 0 and data[end - 1] == 0:
        end -= 1
    return data[:end]


def _intersect(a, b, lo, hi):
    """[a,b) と [lo,hi) の共通部分。 空なら None。"""
    x, y = max(a, lo), min(b, hi)
    return (x, y) if x < y else None


def _subtract(a, b, lo, hi):
    """[a,b) から [lo,hi) を引く。 0-2 個の区間を返す。"""
    out = []
    if a < min(b, lo):
        out.append((a, min(b, lo)))
    if max(a, hi) < b:
        out.append((max(a, hi), b))
    return out


class StashPool:
    """裏 RAM ($8000-$EFFF) の空きから退避枠を切り出す。

    最終配置・トランポリン・スタック予約を **最初に取り除いてから** 配る
    ので、 「退避したかけらを、 後で置くコードが踏む」 事故が構造的に
    起きない。 足りなければ確保できる最大の連続領域を添えて失敗する。
    """

    def __init__(self, blocked):
        self.free = [(STASH_LO, STASH_HI)]
        for lo, hi in blocked:
            nxt = []
            for a, b in self.free:
                nxt += _subtract(a, b, lo, hi)
            self.free = nxt

    def alloc(self, size, why):
        for i, (a, b) in enumerate(self.free):
            if b - a >= size:
                self.free[i] = (a + size, b)
                return a
        big = max((b - a for a, b in self.free), default=0)
        raise RuntimeError(
            f'裏 RAM の退避枠が足りない ({why} に {size} byte 必要、 '
            f'空いている最大の連続領域は {big} byte)')


def chunk_spans(start_addr, n_chunks):
    """各チャンクの最終配置 [addr, addr+len) を返す。

    末尾のチャンクはスタック予約 ($FC00) に当たったら切り詰める。 切り詰めで
    本体が欠けるなら simulate_plan が突合せで検出して失敗させる。
    """
    spans = []
    for i in range(n_chunks):
        t = start_addr + i * CHUNK_SIZE
        if t >= RESIDENT_END:
            raise RuntimeError(
                f'チャンク #{i} の最終配置 ${t:04X} が常駐上限 '
                f'${RESIDENT_END:04X} を超えている')
        spans.append((t, min(CHUNK_SIZE, RESIDENT_END - t)))
    return spans


def plan_passes(start_addr, n_chunks):
    """テープ順のパス記述を返す。

    分割の境目は 「毎パス上書きされる範囲 $1400-$5FFF」 だけから導く:

      - チャンク 0 は必ず最後 (LOADM ",,R")。 最終配置が最も低く、 上書き
        範囲の下に潜り込むのはこのチャンクだからである。
      - それ以外は高位のチャンクから順に先に流す。 上書き範囲の **外** に
        落ちる部分はそのまま最終位置へ置き、 **内** に落ちる部分だけを
        裏 RAM へ退避して、 最終パスが (バッファを空けた後で) 戻す。

    どのチャンクのどこが退避対象になるかは、 エントリアドレスと本体サイズ
    だけで決まる。 表を手で書き換える余地はどこにも無い。
    """
    spans = chunk_spans(start_addr, n_chunks)

    blocked = [(t, t + ln) for t, ln in spans]
    blocked.append((STAGE2_ADDR, STAGE2_ADDR + TRAMP_RESERVE))
    pool = StashPool(blocked)

    passes = []
    stashes = []           # (退避先, 戻し先, 長さ) — 最終パスで戻す

    for i in range(n_chunks - 1, 0, -1):
        t, ln = spans[i]
        moves = []
        for a, b in _subtract(t, t + ln, VOLATILE_BEG, VOLATILE_END):
            moves.append((BUFFER_ADDR + (a - t), a, b - a))
        ov = _intersect(t, t + ln, VOLATILE_BEG, VOLATILE_END)
        parked = None
        if ov:
            a, b = ov
            st = pool.alloc(b - a, f'チャンク #{i} の ${a:04X}-${b - 1:04X}')
            moves.append((BUFFER_ADDR + (a - t), st, b - a))
            stashes.append((st, a, b - a))
            parked = (st, a, b - a)
        passes.append({'chunk_idx': i, 'moves': moves, 'entry': None,
                       'is_last': False, 'span': (t, ln), 'parked': parked})

    t, ln = spans[0]
    moves = []
    if t != BUFFER_ADDR:
        moves.append((BUFFER_ADDR, t, ln))
    # 退避したかけらを戻すのは **本体展開の後**。 戻し先は上書き範囲の中、
    # つまりバッファと重なるので、 先に戻すとバッファをまだ読む前に潰す。
    for st, dst, length in reversed(stashes):
        moves.append((st, dst, length))
    passes.append({'chunk_idx': 0, 'moves': moves, 'entry': start_addr,
                   'is_last': True, 'span': (t, ln), 'parked': None})
    return passes


# ===== Per-pass code emission =====

def build_pass(pass_desc, chunks, template):
    """Return (loadm_payload, block, info_dict) for one pass."""
    ci = pass_desc['chunk_idx']
    tramp, run_len = build_trampoline(template, pass_desc['moves'],
                                      pass_desc['entry'])
    block = wrap_block(tramp, chunks[ci])
    info = {
        'chunk_idx': ci,
        'is_last': pass_desc['is_last'],
        'span': pass_desc['span'],
        'moves': pass_desc['moves'],
        'parked': pass_desc['parked'],
        'entry': pass_desc['entry'],
        'tramp_len': run_len,
    }
    payload = make_loadm_payload(STAGER_LOAD_ADDR, block, STAGER_LOAD_ADDR)
    return payload, block, info


# ===== Byte-for-byte verification of the plan =====

def simulate_plan(start_addr, body, passes, blocks):
    """計画を 64 KiB のメモリ像の上で 1 byte ずつ再生し、 結果を突合せる。

    「後続のロードが、 すでに運び終えた塊を踏まないこと」 を目視や場合分けの
    正しさに賭けない。 実際に踏めば最後の突合せが合わなくなるので、 ここで
    必ず落ちる。 併せて、 トランポリン自身・スタック予約・エントリ未満の
    低位領域への書込みも 「その場で」 失敗させる。
    """
    POISON = 0xA5
    mem = bytearray([POISON]) * 0x10000
    written = bytearray(0x10000)

    tramp_lo, tramp_hi = STAGE2_ADDR, STAGE2_ADDR + TRAMP_RESERVE

    def guard(addr, length, what):
        if addr < start_addr:
            raise RuntimeError(
                f'{what}: ${addr:04X} はエントリ ${start_addr:04X} より下位で、 '
                f'BASIC のワークと割込みベクタを踏む')
        if addr + length > RESIDENT_END:
            raise RuntimeError(
                f'{what}: ${addr:04X}+{length} がスタック予約 '
                f'${RESIDENT_END:04X} を踏む')
        if _intersect(addr, addr + length, tramp_lo, tramp_hi):
            raise RuntimeError(
                f'{what}: ${addr:04X}+{length} がトランポリンの確保枠 '
                f'${tramp_lo:04X}-${tramp_hi - 1:04X} を踏む')

    for pi, (p, block) in enumerate(zip(passes, blocks)):
        # 1) LOADM がブロックを $1400 から書き込む
        if STAGER_LOAD_ADDR + len(block) > VOLATILE_END:
            raise RuntimeError(
                f'pass {pi + 1}: LOADM ブロックが ${VOLATILE_END:04X} を超える')
        mem[STAGER_LOAD_ADDR:STAGER_LOAD_ADDR + len(block)] = block
        for a in range(STAGER_LOAD_ADDR, STAGER_LOAD_ADDR + len(block)):
            written[a] = 1

        # 2) トランポリンが移動表どおりに運ぶ (6809 と同じ 1 byte ずつ)
        for src, dst, length in p['moves']:
            what = f'pass {pi + 1} の移動 ${src:04X}->${dst:04X}'
            guard(dst, length, what)
            # 中間パスは最後に復帰小片 ($141A) へ跳ぶ。 移動がそこを潰すと
            # ROM を戻せないまま迷子になる (症状は 「Ready に戻らない」)。
            if p['entry'] is None and _intersect(dst, dst + length,
                                                 RETSTUB_ADDR,
                                                 STAGE2_SRC_BASE):
                raise RuntimeError(
                    f'{what}: 中間パスの復帰小片 ${RETSTUB_ADDR:04X}-'
                    f'${STAGE2_SRC_BASE - 1:04X} を潰している')
            if dst < src:
                for k in range(length):
                    mem[dst + k] = mem[src + k]
                    written[dst + k] = written[src + k]
            elif dst > src:
                for k in range(length - 1, -1, -1):
                    mem[dst + k] = mem[src + k]
                    written[dst + k] = written[src + k]

    got = bytes(mem[start_addr:start_addr + len(body)])
    if got != body:
        for i, (x, y) in enumerate(zip(got, body)):
            if x != y:
                raise RuntimeError(
                    f'転送後のメモリ像が本体と違う: ${start_addr + i:04X} が '
                    f'${x:02X} (本体は ${y:02X})。 '
                    f'後続のロードがすでに運んだ塊を踏んでいる')
        raise RuntimeError('転送後のメモリ像の長さが本体と違う')
    if not all(written[start_addr:start_addr + len(body)]):
        raise RuntimeError('本体の一部が一度も書かれていない')
    return True


# ===== Procedure text =====

def build_txt(start_addr, n_chunks, tape_files, t77_name, real_size,
              loader_lines=None):
    lines = []
    lines.append(f"=== {t77_name} ロード手順 (FM-7 F-BASIC) ===")
    lines.append("")
    lines.append(f"  エントリアドレス     : ${start_addr:04X}")
    lines.append(f"  実データ             : {real_size} bytes "
                 f"({real_size / 1024:.1f} KiB)")
    lines.append(f"  16 KiB チャンク数    : {n_chunks}")
    lines.append(f"  テープのパス数       : {len(tape_files)}")
    pad_total = n_chunks * CHUNK_SIZE - real_size
    if pad_total > 0:
        lines.append(f"  末尾ゼロパディング   : {pad_total} bytes")
    lines.append("")
    lines.append("  最終メモリ配置:")
    for info in sorted(tape_files, key=lambda x: x['chunk_idx']):
        a, ln = info['span']
        lines.append(f"    チャンク#{info['chunk_idx']}  "
                     f"-> ${a:04X}-${a + ln - 1:04X}")
    lines.append("")
    lines.append("  (PC 側で D77 -> T77/WAV/TXT を変換する手順はリポジトリの README.md を参照)")
    lines.append("")
    if loader_lines:
        lines.append("──────────────────────────────────────────────────")
        lines.append(" 操作手順 — 打つのはこの 1 行だけ")
        lines.append("──────────────────────────────────────────────────")
        lines.append("")
        lines.append("  RUN \"CAS0:\"")
        lines.append("")
        lines.append("  テープの先頭に BASIC ローダーが載っている。 これを読んで")
        lines.append("  走らせるだけで、 後は最後まで自動で進む。")
        lines.append("  読み終わるとそのままタイトル画面が出る。")
        lines.append("")
        total_secs = sum(_pass_seconds(i) for i in tape_files)
        lines.append(f"  読み込みに掛かる時間は 全部で 約 {total_secs / 60:.0f} 分 "
                     f"({total_secs:.0f} 秒)。")
        lines.append("  プログラムから呼んだ LOADM は Searching / Found: を出さない")
        lines.append("  ので、 この間は画面が止まったままに見えるが正常である")
        lines.append("  (ローダーが最初に LOADING の案内を出す)。")
        lines.append("")
        lines.append(f"  ローダーの中身 (\"{LOADER_NAME}\"):")
        for l in loader_lines:
            lines.append(f"      {l}")
        lines.append("")
        lines.append("  ※ この 1 行版は実機のカセット入力での起動を確認済み")
        lines.append("     (2026-08-15)。 うまく行かない場合は下の")
        lines.append("     「手で打つ手順」 を使うこと。")
        lines.append("")
    lines.append("──────────────────────────────────────────────────")
    if loader_lines:
        lines.append(" 手で打つ手順 (1 行版が通らない時)")
    else:
        lines.append(" 操作手順 (F-BASIC の Ready プロンプトで以下を順に入力)")
    lines.append("──────────────────────────────────────────────────")
    lines.append("")
    if loader_lines:
        lines.append("  ※ **ファイル名まで打つこと。** テープの先頭には BASIC の")
        lines.append(f"     ローダー (\"{LOADER_NAME}\") が載っており、 名前を省いた")
        lines.append("     LOADM \"CAS0:\" はそれに当たって Bad File Mode になる。")
        lines.append("     名前を付けると Skip: LOADER と出して読み飛ばしてくれる")
        lines.append("")
    lines.append(f"  CLEAR ,&H{CLEAR_VALUE:04X}")
    lines.append("")
    for i, info in enumerate(tape_files):
        idx = f"[{i + 1}/{len(tape_files)}]"
        tail = ", 最終" if info['is_last'] else ""
        lines.append(f"  {idx} {info['name']}  チャンク#{info['chunk_idx']}"
                     f"{tail}")
        dev = f"CAS0:{info['name']}" if loader_lines else "CAS0:"
        if info['is_last']:
            lines.append(f"        LOADM \"{dev}\",,R")
        else:
            lines.append(f"        LOADM \"{dev}\"")
            lines.append(f"        EXEC &H{STAGER_LOAD_ADDR:04X}")
        # このパスをテープに流すのに掛かる実時間 (テープ波形の長さそのもの)。
        # 読み込み中は画面が止まったままなので、 目安が無いと固まったと
        # 思われてしまう。
        # 半サイクルの上位 bit は極性フラグ (POL) なので、 長さだけを取り出す。
        secs = _pass_seconds(info)
        lines.append(f"        ; 読み込みに約 {secs / 60:.0f} 分 ({secs:.0f} 秒) 掛かる。")
        lines.append(f"        ;   Found: {info['name']} が出た後は画面が止まったままだが、")
        lines.append("        ;   テープが回っている間は正常。 Ready が出るまで待つ")
        lines.append("        ; トランポリンの動作:")
        for src, dst, ln in info['moves']:
            if src == dst:
                lines.append(f"        ;   ${src:04X}-${src + ln - 1:04X} "
                             f"は既に定位置 (移動しない)")
                continue
            way = "前方" if dst < src else "後方"
            lines.append(f"        ;   ${src:04X}-${src + ln - 1:04X} -> "
                         f"${dst:04X}-${dst + ln - 1:04X}  {ln} byte ({way})")
        if info['parked']:
            st, back, ln = info['parked']
            lines.append(f"        ;   (${st:04X} は裏 RAM の退避枠。 最終パスが "
                         f"${back:04X} へ戻す)")
        if info['is_last']:
            lines.append(f"        ;   LDS #${ENTRY_STACK:04X} / "
                         f"JMP ${info['entry']:04X}")
        else:
            lines.append("        ;   ROM オーバレイを戻して RTS "
                         "(= F-BASIC へ復帰)")
        lines.append("")
    lines.append("──────────────────────────────────────────────────")
    lines.append(" 補足")
    lines.append("──────────────────────────────────────────────────")
    lines.append("")
    lines.append(f"  - CLEAR ,&H{CLEAR_VALUE:04X} で MEMSIZ を $13FF に下げ、")
    lines.append("    $1400-$7FFF をユーザ作業域として確保する")
    lines.append("")
    lines.append("  - 各 LOADM ブロックは単一連続 ($1400-$5FFF) で構成:")
    lines.append(f"      ${STAGER_LOAD_ADDR:04X}-${RETSTUB_ADDR - 1:04X}  "
                 f"Stage 1                      (26 B)")
    lines.append(f"      ${RETSTUB_ADDR:04X}-${STAGE2_SRC_BASE - 1:04X}  "
                 f"復帰小片                      ( 6 B)")
    lines.append(f"      ${STAGE2_SRC_BASE:04X}-....   Stage 2 source + 移動表")
    lines.append("      ....-$1FFF   ゼロパディング")
    lines.append("      $2000-$5FFF  16 KiB バッファ")
    lines.append("")
    lines.append("  - Stage 1 は IRQ マスク + ROM OFF + Stage 2 と移動表を")
    lines.append(f"    ${STAGE2_ADDR:04X} (裏 RAM) へコピーして JMP。Stage 2 は移動表を")
    lines.append("    そのとおり実行し、最終パスなら LDS + JMP entry、")
    lines.append(f"    中間パスなら ${RETSTUB_ADDR:04X} の復帰小片へ跳ぶ")
    lines.append("")
    lines.append(f"  - 復帰小片が低位 RAM (${RETSTUB_ADDR:04X}) に居るのは、")
    lines.append(f"    ${STAGE2_ADDR:04X} が ROM オーバレイの下だからである。そこで")
    lines.append("    オーバレイを戻すと次の命令が ROM から取られてしまう")
    lines.append("")
    lines.append(f"  - 裏 RAM の退避枠は ${STASH_LO:04X}-${STASH_HI - 1:04X} から")
    lines.append("    切り出す (ROM オーバレイ OFF で素の RAM になる領域)。")
    lines.append("    最終配置・トランポリン・スタック予約を除いてから配るので、")
    lines.append("    退避したかけらを後続のロードが踏むことはない")
    lines.append("")
    lines.append("  - EXEC は必ず明示的にアドレスを指定すること")
    lines.append("    (引数なし EXEC は実機で挙動が不安定)")
    lines.append("")
    lines.append("  - 1 行打つごとに Ready が出るのを待ってから次を打つこと。")
    lines.append("    前の行の処理中に打った文字は取りこぼされる")
    lines.append("")
    lines.append("  - デバイス名は \"CAS0:\" と必ず明示して打つこと。")
    lines.append("    デバイス名を省いた LOADM \"\",,R は使わない")
    lines.append("    (実機で Missing Operand になり読み込めなかった)")
    lines.append("")
    lines.append("  - 綴りを間違えたときの出方 (いずれも実測):")
    lines.append("      \"CAS:\"    0 落ち          -> Bad File Descriptor")
    lines.append("      \"CASO:\"   0 が英字の O    -> Device Unavailable")
    lines.append("      \"CAS0\"    : 落ち          -> CAS0 という名前のファイルを")
    lines.append("                                   探し続け、 Skip: を繰り返して")
    lines.append("                                   いつまでも止まらない")
    lines.append("      CAS0:     引用符が無い     -> Type Mismatch")
    lines.append("    どれも打ち直せばよい。 テープを頭から巻き戻すこと")
    lines.append("")
    return "\n".join(lines) + "\n"


# ===== Main =====

def parse_addr(s):
    s = s.strip()
    try:
        if s.lower().startswith('0x') or s.lower().startswith('&h'):
            v = int(s[2:], 16)
        elif s.startswith('$'):
            v = int(s[1:], 16)
        else:
            v = int(s, 0)
    except ValueError:
        print(f'error: アドレス/数値の書式が不正です: {s!r} '
              f'(例: 0x0200 / $0200 / &H0200 / 512)', file=sys.stderr)
        sys.exit(1)
    if not 0 <= v <= 0xFFFF:
        print(f'error: アドレス/数値が $0000-$FFFF の範囲外です: {s!r}',
              file=sys.stderr)
        sys.exit(1)
    return v


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('src', help='input D77 file')
    ap.add_argument('--addr', required=True, type=parse_addr,
                    help='entry / load-start address (e.g. 0x0200)')
    ap.add_argument('--skip', type=parse_addr, default=256,
                    help='bytes to skip from extracted payload (default 256, '
                         'i.e. one sector — typical FM-7 IPL boot sector at '
                         'C0 H0 R1 is skipped automatically; pass --skip 0 '
                         'to disable)')
    ap.add_argument('--size', type=parse_addr, default=None,
                    help='bytes to use from payload (default: all)')
    ap.add_argument('-o', '--out', default=None, help='output T77 path')
    ap.add_argument('-t', '--txt', default=None, help='output TXT procedure path')
    ap.add_argument('-w', '--wav', default=None,
                    help='output WAV path (default: <src>.wav). '
                         'Use --no-wav to skip WAV generation.')
    ap.add_argument('--no-wav', action='store_true',
                    help='do not emit a WAV file alongside the T77')
    ap.add_argument('--silence', type=float, default=5.0,
                    help='WAV silence (sec) at head, between LOADMs, and tail '
                         '(default: 5.0)')
    ap.add_argument('--no-wav-silence-cue', action='store_true',
                    help='omit the 0x0000 inter-file cue marker in the T77')
    ap.add_argument('--no-basic-loader', action='store_true',
                    help='do not put the ASCII BASIC loader at the head of the '
                         'tape (the user then types CLEAR + one LOADM per pass '
                         'by hand, as before)')
    ap.add_argument('--tramp', required=True,
                    help='trampoline template (Stage 1 + Stage 2) assembled '
                         'from scripts/trampoline.asm by the Makefile')
    args = ap.parse_args()

    if not os.path.isfile(args.src):
        print(f'error: input not found: {args.src}', file=sys.stderr)
        return 1
    if not os.path.isfile(args.tramp):
        print(f'error: trampoline template not found: {args.tramp}',
              file=sys.stderr)
        return 1
    with open(args.tramp, 'rb') as f:
        template = f.read()

    base, _ = os.path.splitext(args.src)
    out_t77 = args.out or (base + '.t77')
    out_txt = args.txt or (base + '.txt')
    out_wav = args.wav or (base + '.wav')

    with open(args.src, 'rb') as f:
        d77 = f.read()

    # --size given => honor it exactly, skip the auto-trim heuristic.
    # --size absent => trim trailing fill tracks ($00 / $E5 / $FF formatter
    # fill) so we don't see the empty back half of the floppy.
    trim = (args.size is None)
    payload = extract_d77_payload(d77, trim_trailing_fill=trim, verbose=True)
    print(f'[+] D77 payload          : {len(payload)} bytes '
          + ('(auto-trim: trailing fill tracks dropped)' if trim
             else '(raw extract, no auto-trim because --size was given)'))

    body = payload[args.skip:]
    if args.size is not None:
        body = body[:args.size]
    real_size = len(body)
    print(f'[+] working binary       : {real_size} bytes '
          f'(skip={args.skip}, size={args.size if args.size is not None else "auto"})')

    if real_size == 0:
        print('error: working binary is empty', file=sys.stderr)
        return 1

    chunks = split_into_chunks(body)
    n = len(chunks)
    print(f'[+] split into           : {n} x 16 KiB chunk(s)')

    if args.addr < LOADM_LO_LIMIT:
        print(f'note: entry address ${args.addr:04X} is below LOADM lower limit '
              f'${LOADM_LO_LIMIT:04X}, but it is reached by the trampoline copy '
              f'so this is fine.', file=sys.stderr)

    try:
        plan = plan_passes(args.addr, n)
        tape_files = []
        blocks = []
        for i, p in enumerate(plan):
            loadm, block, info = build_pass(p, chunks, template)
            info['name'] = f'C{i + 1:02d}'
            info['loadm'] = loadm
            tape_files.append(info)
            blocks.append(block)
        # 生成物を書き出す **前に**、 計画を 1 byte ずつ再生して突合せる。
        simulate_plan(args.addr, body, plan, blocks)
    except (NotImplementedError, RuntimeError) as e:
        print('error: CMTロード不可 — テープ多段ロードの収容上限を超えています。',
              file=sys.stderr)
        print(f'       エントリ ${args.addr:04X} / {n} チャンク '
              f'({real_size} byte) では配置が成立しません。', file=sys.stderr)
        print(f'       詳細: {e}', file=sys.stderr)
        print(f'       対策: 本体を減らすか、 エントリを ${BUFFER_ADDR:04X} 以上へ '
              f'移して最終配置を上へ逃がす。', file=sys.stderr)
        return 1

    print(f'[+] plan_passes -> {len(plan)} pass(es):')
    for i, info in enumerate(tape_files):
        tail = ' LAST' if info['is_last'] else ''
        a, ln = info['span']
        print(f'    tape[{i + 1}/{len(plan)}]  {info["name"]}  '
              f'chunk#{info["chunk_idx"]} -> ${a:04X}-${a + ln - 1:04X}'
              f'  移動 {len(info["moves"])} 手 / トランポリン '
              f'{info["tramp_len"]} byte{tail}')
        for src, dst, mlen in info['moves']:
            way = '定位置' if src == dst else ('前方' if dst < src else '後方')
            print(f'        ${src:04X} -> ${dst:04X}  {mlen} byte  ({way})')
    print('[+] plan verified        : 64 KiB のメモリ像で再生し、 '
          f'${args.addr:04X} に本体 {real_size} byte が揃うことを確認')

    # テープに載せるファイルの並び。 **BASIC ローダーを先頭に置く**ので、
    # 利用者が打つのは RUN "CAS0:" の 1 行だけで済む (Issue #40)。
    total_secs = sum(_pass_seconds(info) for info in tape_files)
    loader_lines = (None if args.no_basic_loader
                    else basic_loader_lines(len(tape_files), total_secs))
    tape_seq = [(info['name'], 'loadm', info['loadm']) for info in tape_files]
    if loader_lines is not None:
        tape_seq.insert(0, (LOADER_NAME, 'basic', loader_lines))
        print(f'[+] BASIC loader         : "{LOADER_NAME}" '
              f'{len(loader_lines)} 行 (テープ先頭)')
        for l in loader_lines:
            print(f'        {l}')

    t77 = build_t77(tape_seq,
                    mark_inter_file_silence=not args.no_wav_silence_cue)
    with open(out_t77, 'wb') as f:
        f.write(t77)
    print(f'\n[+] T77 written          -> {out_t77} ({len(t77)} bytes)')

    txt = build_txt(args.addr, n, tape_files,
                    os.path.basename(out_t77), real_size, loader_lines)
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write(txt)
    print(f'[+] procedure written    -> {out_txt}')

    if not args.no_wav:
        wav = build_wav(tape_seq,
                        head_silence=args.silence,
                        gap_silence=args.silence,
                        tail_silence=args.silence)
        with open(out_wav, 'wb') as f:
            f.write(wav)
        dur = (len(wav) - 44) / WAV_SAMPLE_RATE / 2   # 16-bit mono
        print(f'[+] WAV written          -> {out_wav}  '
              f'({len(wav)} bytes, ~{dur:.1f} s, {WAV_SAMPLE_RATE} Hz 16-bit mono, '
              f'{args.silence:.1f}s silence head/gaps/tail)')

    print('\n--- procedure summary ---')
    if loader_lines is not None:
        print('  RUN "CAS0:"        ; 打つのはこの 1 行だけ (先頭の BASIC ローダー)')
        print('  --- 1 行版が通らない時に手で打つ手順 ---')
    print(f'  CLEAR ,&H{CLEAR_VALUE:04X}')
    for i, info in enumerate(tape_files):
        dev = f'CAS0:{info["name"]}' if loader_lines is not None else 'CAS0:'
        if info['is_last']:
            print(f'  LOADM "{dev}",,R    ; tape[{i+1}] chunk#'
                  f'{info["chunk_idx"]} (最終)')
        else:
            print(f'  LOADM "{dev}"       ; tape[{i+1}] chunk#'
                  f'{info["chunk_idx"]}')
            print(f'  EXEC &H{STAGER_LOAD_ADDR:04X}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
