---
name: disk-boot-rescue
description: 「起動できない / パーティションが見えない / RAID が組めない」Linux GPT+RAID サーバの診断と修復。LBA0 protective MBR (0xEE) 検証、md スーパブロックの沈黙失敗解析、PXE ネットワーク救援ブリッジ構築、バックアップ削除前の冗長性検証を行う。対象ホストが起動不能・再起動ループ、/proc/partitions に sdaN 不存在、mdadm が警告なしで -EINVAL、UEFI ブートエントリが正しく見えるのに起動しない、大容量バックアップの削除前に冗長性を証明したい場合に使う。 (GPT/MBR boot failure, kernel partition parsing, md superblock repair, PXE rescue bridge, backup redundancy verification)
---

# disk-boot-rescue

データを持ちながら起動不能になったサーバの診断・修復プレイブック。実障害（62GiB データ救出 → 5 日でローカル起動回復）から抽出した手法集。

**基本姿勢: read-only 最優先、write は最終手段。** すべての書込には pre-check（事前状態の期待値）・readback 検証・書かなかった場合の理由記録が必須。

## 使う場面

- ホストが起動不能（再起動ループ / no bootable device / 起動後数分で落ちる）
- GPT が `gdisk -l` 上は正常なのに、カーネルにパーティションが認識されない
- `mdadm --assemble` が `-EINVAL` で失敗し、カーネルに警告メッセージがない
- UEFI ブートエントリが「正しく」見えるのに、メディア挿在時のみ起動する（ファームウェア非標準）
- ホストは死んでいるがネットワークで到達可能（PXE 救援が必要）
- 大容量バックアップ（数十 GiB の tar 等）を削除する前に冗長性を証明したい

## 素早い分岐

| 症状 | 最初の一手 | 詳細 |
|---|---|---|
| GPT パーティションが見えない | `sudo python3 scripts/check_lba0.py /dev/sdX`（read-only）で LBA0 の 446-509 を見る | `references/partition-parsing.md` §1 |
| mdadm -EINVAL（警告なし） | md スーパブロックを raw ダンプし pad0/pad3 と csum を直接検査 | `references/partition-parsing.md` §3 |
| メディア/USB 在位時のみ起動 | efivarfs の生バイト + `BootCurrentDP` で確定（efibootmgr の表示を信用しない） | `references/partition-parsing.md` §5 |
| ホスト死、LAN 到達可 | PXE ブリッジを構築（dnsmasq + 手組み initrd） | `references/pxe-bridge.md` |
| 大容量バックアップを削除したい | `python3 scripts/verify_backup_redundancy.py backup.tar <live-dir>` | `references/data-verification.md` |

## コア・ルール（作業前に必ず読む）

1. **カーネルのパーティション解析はブート経路非依存。** UEFI でも legacy でもカーネルは LBA0 を読む。`msdos.c` は 4 レコードを**インデックス順**に走査し、**0xEE レコードに先に到達した時だけ** GPT パーサに譲る（先に有効な非 0xEE レコードを登録すれば領有 = GPT 無視）。ファクトリ構成・legacy 残骸等の「有効に見える非 protective な MBR レコード」が LBA0 にあると **GPT が完全に無視される**（→ パーティション不存在 → RAID 組立不能）。逆に **LBA0 がゼロ化なら無害**（msdos が棄権 → GPT が解析される）。
2. **識別は必ず UUID で。** `/dev/sdX` のラベルは起動間で入れ替わる（同一 PARTUUID のディスクでは特に）。
3. **検証は O_DIRECT + 別経路。** 書き込みと同じコマンドで読み戻すだけではページキャッシュの偽陽性を検出できない。最終判定は別経路（例: `mdadm --examine` = カーネルの読込経路）で。
4. **パーティションデバイスに対する seek/skip はパーティション先頭からの相対値**であり、ディスク絶対セクタではない。「相対何を」を常に明記する。
5. **唯一のコピーを `/tmp` に置かない。** 再起動で消える場所は作業領域専用。
6. **証拠を削除する前に「現行システムがまだ保持しているか」を確認**（journal の保持ウィンドウ、GPT の再抽出可能性等）。唯一性が消えてからでは手遅れ。
7. **本スキルは read-only 工具のみ同梱。** ディスク書込（パーティション表 / ブートローダ修復）は対象ごとに設計する。設計パターン（expect/patch + 分類 FATAL）は `references/pxe-bridge.md` §5 を参照。

## 作業フロー

1. **証拠収集（read-only）**: `/proc/partitions`、`dmesg`（GPT/md/ata 行）、`blkid`、`mdadm --examine` 全メンバー、LBA0 + GPT（プライマリ/バックアップ）の raw ダンプ + CRC 検証
2. **仮説照合**: 上の分岐表に当てる。「GPT が壊れている」と仮定する前に **必ず LBA0 を見る**（本スキルの最重要の教訓: 壊れているのは GPT ではなく LBA0 の「ゲート」だったケースが実在する）
3. **修復設計**: 事前状態の期待値（pre-check）→ パッチ → 事後状態の期待値（readback）をバイト単位で固定。pre-check 失敗時は**書かない**（理由を分類して記録: 復旧不能 / 意図的データと区別できない / 物理エラー疑い）
4. **テスト優先**: 実ディスクに触る前に、sparse イメージで同一レイアウト + 同一障害を再現する合成ディスクで修復スクリプトを回し、FATAL 全分岐を含む回帰を通す
5. **適用 + 検証**: 書込 → O_DIRECT readback → 別経路検証 → 再起動 → **ブート経路そのものを検証**（例: `/proc/cmdline` がローカル由来か PXE 由来か）
6. **文書化**: タイムライン、根本原因、再発対策をランブックに残す。「1 回直った」は完了ではない（再発のトリガーが未解明なら監視を追加する）

## ファイルマップ

- `references/partition-parsing.md` — カーネルのパーティション解析ルール（0xEE）、GPT バイトレイアウト + CRC、md v1.2 スーパブロック、GRUB legacy レイアウト、NVRAM フォレンジクス
- `references/pxe-bridge.md` — PXE 救援ブリッジ（dnsmasq の沈黙原因 2 つ、initrd 手組みレシピ、自動修復フロー設計）
- `references/data-verification.md` — バックアップ冗長性検証手法（方向性のルール、bsdtar/GNU tar の罠）+ 資産の 3 分類
- `references/case-study.md` — 匿名化実案例（5 日間で 62GiB 救出 → 根本原因 → 自動修復 → 安定稼働）
- `scripts/check_lba0.py` — LBA0 + GPT 両側の read-only 診断（判定タグ + exit code。定期 watchdog にも使える）
- `scripts/verify_backup_redundancy.py` — tar ↔ 実ディレクトリの (サイズ, パス) 全件照合（削除可否の判定）

## 安全性

- スクリプトはすべて `O_RDONLY` で開き、**書き込みは一切行わない**
- 実ディスクの raw 読みは root 権限を必要とする（診断目的の 1 回読み）
- 本スキルのどの手順も、pre-check/readback/別経路検証の 3 点セットを省略してはならない
