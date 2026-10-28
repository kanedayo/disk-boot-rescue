# バックアップの冗長性検証と削除判断

大容量バックアップ（数十 GiB の tar 等）を削除する前に、「実データ側（live）がすべて保持している」ことを**ファイル単位**で証明する手法。

## 1. 方向性のルール（最重要）

照合は (size, path) の**集合差**で行うが、**削除可否に影響するのは片方向だけ**:

- **tar にあって live に見えない（missing）** → 削除不可（この tar だけがそのファイルの最後のコピー）
- **live にあって tar に見えない（extra）** → 削除に無関係（tar は live より古いだけ。報告はするがブロックしない）
- **size 不一致（mismatch）** → 削除不可（tar 側が新しい可能性あり = 方向を確認せよ）

「live に追加ファイルがある」だけで「バックアップは不要」とはならない。逆もまた然り — **tar の側が欠落するまでは、この tar は安全に削除できない**。

## 2. (size, path) 照合のやり方（awk 使わない）

### 2.1 tar 側の listing

```
LC_ALL=C tar -tvf backup.tar
```

- **`LC_ALL=C` が必須**: 非 ASCII（日本語等）ファイル名が**オクテスケープ**（`\343\202\254` 形式）になる。そのままでは live 側（実 UTF-8）と一致しない
- 行フォーマット（bsdtar / libarchive）:
  `mode nlink uid gid size mon day hh:mm:ss path`
  → size = 第 5 フィールド、path = 第 9 フィールド以降
- **GNU tar の出力は別**:
  `mode user/group size mon day hh:mm:ss year path`
  → size = 第 3 フィールド、path = 第 8 フィールド以降（year が 1 つ多い）
- どちらでも**先頭 field2（owner）に `/` が含まれれば GNU tar** で判別できる
- 先頭が `-` で始まらない行（ディレクトリ行 `drwx...` 等）はスキップ

### 2.2 live 側の listing

- **GNU find（Linux）**: `find <live-dir> -type f -printf '%s %P\n'`（`%P` = ルートからの相対パス）
- **macOS（BSD find に `-printf` 無し）**: `find <live-dir> -type f -print0` + `stat -f '%z %N'`（バッチ化。1000 件ずつ）
- **awk の連続空白マージが壊す**: パス内のスペース（例: 日本語ファイル名の間に空白）は awk フィールド処理で**連続空白が 1 つに畳まれる** → 実在するファイルが「欠落」に見える偽陽性が出た実例あり。**maxsplit 付き split（Python `line.split(None, 8)`）で解析**し、パスの残りは空白をそのまま残す

### 2.3 AppleDouble のノイズ

- live 側（macOS 由来）に `._*` / `.DS_Store` が多数出るのは**正常**。比較対象から除外（または noise として報告）
- 逆に tar 側に `._*` が入っていても無害

## 3. macOS / bsdtar の実測挙動

- **bsdtar（macOS）はアーカイブ作成時に `._*` AppleDouble ファイルを既定で除外する**（`.DS_Store` は入れる）。したがって live 側に `._*` がある場合、tar には無い → extra(noise) として報告されるが**無害**（削除判断にブロックしない）
- `tar -tvf` の日付部は libarchive のバージョンで **2〜4 トークン**（`Mon dd hh:mm:ss` / `... hh:mm:ss yyyy` / `yyyy-mm-dd hh:mm`）に変動する。フィールド位置の固定ではなく、日付パターンを認識して path を抽出する方式で（`scripts/verify_backup_redundancy.py`）
- listing は必ず `LC_ALL=C` 付き（非 ASCII → オクテスケープ。連続した `\NNN` を **まとめて** 1 つの UTF-8 バイト列として復号する — バイトごとの `chr()` 変換は多文字コード文字を壊す）

## 4. フレッシュネスと整合性の補助チェック

- **totals 対比**: tar の全ファイル合計サイズ ≒ live 側の合計サイズ（差 = extra/missing の合計で説明できるはず）
- **live 側が新しいか**: 削除前に「live 側が tar 側と同等かそれより新しい」ことを確認（mtime 比較や、対象ファイルが live で書き換え履歴を持っているか）
- **証拠の保持確認**: 削除しようとしているものが**唯一の証拠**（journal の故障ウィンドウ等）になっていないか。実システム側がまだ同じウィンドウを保持している（例: journal の保持期間）ことを確認してから削除。唯一性が消えてからでは手遅れ

## 5. 資産の 3 分類（削除/保持の判断枠）

| 分類 | 例 | 判定 |
|---|---|---|
| **refetchable**（再取得可能） | live ホストから `scp` できるバイナリ、apt で再インストールできるもの | 削除 OK。再取得手順を文書化 |
| **regenerable**（再生成可能） | スクリプトで生成できる GPT/パーティション表、build 成果物 | 削除 OK。再生成手順を文書化 |
| **unique**（唯一） | カスタム救援スクリプト、GPT/MBR テンプレート、故障時の raw ダンプ | **保持必須**。文書的な場所（/tmp 以外）へ移動 |

**再構築コストを必ず推定して文書化する**（例: 「再構築 30-60 分」「scp で数分」）。コストが低い refetchable は削除してよく、unique は保持する。

## 6. 削除チェックリスト

- [ ] (size, path) 集合差で **missing = 0**（tar にあって live に無いものが 1 件もない）
- [ ] **size mismatch = 0**（または、mismatch が live 側の方が新しいと確認済み）
- [ ] **totals 対比**で差が説明できる（extra の合計と一致）
- [ ] **証拠の保持確認**（この tar が唯一の故障証拠になっていない）
- [ ] **資産 3 分類**で unique な中身が含まれていない
- [ ] 削除前に **fsck 相当の整合性確認**（必要なら tar の `tar -tf` 全走査でアーカイブ自体が壊れていないことを確認）
- [ ] 削除後、**同じスクリプトを再実行して missing=0 を再確認**（削除が live 側を傷つけないことの最終保証）

`scripts/verify_backup_redundancy.py` は上の §2 と §6 を自動化する（missing/mismatch/extra の分類 + exit code）。
