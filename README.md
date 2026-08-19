# HorizonRunner

初代 FM-7 向けの擬似 3D シューティングゲームです。

## ライセンス

MIT License (Copyright (c) 2026 Naomitsu Tsugiiwa)。
全文は [LICENSE](LICENSE)。

## 由来

ビルド基盤とテープ変換は同一作者の公開 MIT リポジトリ https://github.com/7032/FM7BaseCode および https://github.com/7032/D77TOT77WAV から取り込み・改変したものです (ライセンス全文は [scripts/D77TOT77WAV.LICENSE.txt](scripts/D77TOT77WAV.LICENSE.txt) に同梱)。

本作は個人の自主制作物です。
本文中の機種名等は互換性の説明のために記載しており、各権利者との提携・承認関係はありません。

プログラム・グラフィック・面構成・音楽・フォント字形はすべて本リポジトリのソースとして収録した自作です。

## ビルド

cmoc / lwasm / lwlink / python3 が必要です。
`make` で `disks/` の配布物を生成します。

## 配布物 (disks/)

| ファイル | 中身 |
|---|---|
| `disks/horizonrunner.d77` | ディスクイメージ |
| `disks/horizonrunner.t77` | カセットテープ (CMT) イメージ |
| `disks/horizonrunner.wav` | カセットロード用の FSK 音声 |
| `disks/horizonrunner.cmt.txt` | テープからのロード手順 |
