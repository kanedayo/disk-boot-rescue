# disk-boot-rescue

[Claude Code スキル](https://docs.anthropic.com/en/docs/claude-code/skills) — データがありながら起動できない Linux サーバ(GPT + RAID)の診断・修復プレイブック。実障害から抽出: 62 GiB の RAID1 データを 1 日で救出、5 日目で根本原因特定(LBA0 のファクトリ MBR が GPT をカーネルに隠していた)、1 PXE サイクルで自動修復を適用し、ローカル起動が安定稼働した実績がある。

## 役立つ場面

- ホストが起動不能(再起動ループ / no bootable device)だが、データは失いたくない
- `gdisk -l` 上は正常なのに、カーネルが GPT パーティションを認識しない
- `mdadm --assemble` が警告なしの裸の `-EINVAL` で失敗する
- USB/ISO を挿在した時のみ起動する(非標準ファームウェアの NVRAM)
- ホストは死んでいるが LAN 到達可能(PXE 救援が必要)
- 大容量バックアップ tar を削除する**前**に冗長性を証明したい

## インストール

```sh
cp -r disk-boot-rescue ~/.claude/skills/
```

(またはプラグインの `skills/` ディレクトリに配置。スキルは会話内容がトリガー語に一致すると発動し、`SKILL.md` がエントリポイント、`references/` は必要時に読み込まれる。)

## ファイル構成

| ファイル | 内容 |
|---|---|
| `SKILL.md` | エントリポイント: 分岐表、コア・ルール、作業フロー |
| `references/partition-parsing.md` | カーネルのパーティション解析(0xEE ルール、バイト単位の正確な定義)、GPT レイアウト + CRC、md v1.2 スーパブロック、GRUB legacy レイアウト、NVRAM フォレンジクス |
| `references/pxe-bridge.md` | PXE 救援ブリッジ: dnsmasq の沈黙原因、initrd 手組みレシピ、自動修復設計(expect/patch + 分類 FATAL) |
| `references/data-verification.md` | バックアップ冗長性検証手法(方向性のルール、tar listing の罠)、資産の 3 分類、削除チェックリスト |
| `references/case-study.md` | 匿名化実案例(D0–D5)と 10 の教訓 |
| `scripts/check_lba0.py` | LBA0 + GPT 両側の read-only 診断(判定タグ + exit code。定期 watchdog にも使える) |
| `scripts/verify_backup_redundancy.py` | tar ↔ 実ディレクトリの (size, path) 照合。exit 0 = 削除可 |

## 安全性

- **両スクリプトとも厳密に read-only**(`O_RDONLY`。tar 側は `tar -tvf` / `find` / `stat` の実行のみ)。スキル内にはディスク書込は一切同梱されていない — 修復の書込経路は `references/pxe-bridge.md` §5 の expect/patch パターンで**対象ごとに設計**し、常に合成ディスクイメージ上で先にテストする
- 実ブロックデバイスの raw 読みは root 権限が必要(診断目的の 1 回読み)

## プライバシー / PII 除去

本スキルは実障害から抽出したものであり、公開前に以下を除去した:

- **除去**: ホスト名、IP アドレス、MAC アドレス、ディスクシリアル、パーティション/ディスク/アレイの UUID、ユーザーのホームパス、会社・データ名、セッション識別子
- **置換**: すべての日付を相対日付(D0–D5)に。例示の IP は汎用プレースホルダ(RFC 5737 の 192.0.2.x)に
- **保持**: 製品型番(公開仕様)、バイトオフセット、アルゴリズムの記述、実測の性能数値

自分の障害データを適用作業に使う場合は、コミット前に同じカテゴリを再確認すること。

## ライセンス

MIT License(詳細は `LICENSE` ファイルを参照)。コードは Python 3 標準ライブラリのみ。ドキュメントは公開済みのカーネル/UEFI/GRUB の挙動を説明している。
