# パーティション解析とディスク構造（カーネルレベル）

## 1. カーネルのパーティション表選択 — 「GPT が正しく見えるのに見えない」問題

カーネル (`block/partitions/core.c`) はディスクのパーティション表を、登録されたパーサ（handler）の順に試す。**最初に「領した」パーサが勝つ**。

`block/partitions/msdos.c` の `msdos_partition()` の判定:

msdos.c は 4 レコードを**インデックス順に**走査し、**先に遭遇した方が勝つ**: 0xEE レコードに到達すれば即 `return 0`（棄権）、有効な非 0xEE レコードを登録すれば `ret = 1`（領有）。

| LBA0 の状態 | msdos.c の挙動 | 結果 |
|---|---|---|
| 55AA あり + 有効な非 0xEE レコードあり（**0xEE 無し**） | **msdos が領する**（その MBR レコードのみ登録） | **GPT パーサ (`efi.c`) は呼ばれない → GPT パーティションは全部存在しない** |
| 0xEE レコードが**先に**（インデックスが小さい）出現 | 即 `return 0`（棄権） | GPT パーサが解析 → 正常 |
| 有効な非 0xEE レコードが 0xEE より**前に**出現 | 非 0xEE を登録し `ret = 1` | **GPT が無視される**（0xEE が後からあっても救われない） |
| LBA0 がゼロ化（55AA 無し）/ 無効な表 | 棄権 | GPT パーサが解析 → 正常 |
| GPT パーティション型が RAID GUID | `ADDPART_FLAG_RAID` を設定 | カーネルが md を**自動アセンブル**（mdadm.conf 不要） |

**要点**:

- 危険なのは「壊れた GPT」ではなく、**LBA0 に『有効に見える非 protective な MBR テーブル』があること**。ファクトリ構成・legacy 残骸・ハイブリッド MBR 実験の残骸等、0xEE を含まない有効レコードは GPT を完全に隠す。
- **ゼロ化した LBA0 は無害**（msdos が領しない）。したがって「MBR が消えた」より「MBR が復活した」方が危険な場合がある。
- プライマリ GPT が壊れてバックアップ GPT が健全なら、カーネルはバックアップでパーティションを登録する（**書き戻しはしない**。恒久修復は gdisk 等のツールで）。
- **UEFI ブート化しても救われない。** ブート経路（UEFI / legacy）はファームウェア + GRUB の問題であり、カーネルはどちらの経路でも LBA0 からパーティション表を再解析する。LBA0 が汚染されていれば、ESP から UEFI 起動してもカーネル以降は同じ死に方をする（RAID メンバーのパーティションが存在しない → md 組立不能 → root 不可）。
- 検査は 1 行: `sudo xxd -s 446 -l 64 /dev/sdX`。`ee` で始まる（`00 00 00 00 ee ...`）= protective 済（正常）。`00 00 00 00 01 00 ...` のように **0x01 等の有効レコード** = 危険。

## 2. GPT バイトレイアウト

### 2.1 ヘッダ（512B セクタ先頭から）

| オフセット | フィールド | 備考 |
|---|---|---|
| 0-7 | `EFI PART` | シグネチャ |
| 8-11 | revision | 0x00010000 |
| 12-15 | header size | 固定 92 |
| 16-19 | header CRC32 | **検証時はこの 4 バイト（16:20）をゼロ化して** 92 バイトを CRC32 |
| 20-23 | reserved | ゼロ |
| 24-31 | my LBA | **0-based**。プライマリなら 1（= 2 セクタ目。LBA0 は MBR） |
| 32-39 | alternate LBA | バックアップヘッダの LBA（0-based。通常 = 最終 LBA） |
| 40-47 / 48-55 | first / last usable LBA | 0-based |
| 56-71 | disk GUID | |
| 72-79 | partition entry array LBA | 0-based。プライマリなら通常 2 |
| 80-83 / 84-87 | entry 数 / entry サイズ | 通常 128 / 128 |
| 88-91 | entry array CRC32 | 全 entry（n × size バイト）の CRC32 |

**注意**: ヘッダ内すべての LBA フィールドは **0-based**（`gdisk` 等の表示は 1-based に変換するため混乱しやすい）。

バックアップ GPT: **標準**ではヘッダ = 最終 LBA、entry 配列 = その直前 `n*size/512` セクタ。プライマリ/バックアップの entry 内容は**同一**であるはず（my/alternate LBA だけ異なる）。両側の CRC を必ず両方検証する（片方だけ整合 = 部分的な汚染の痕跡）。**カーネルのフォールバックはバックアップを「最終 LBA」から読む**（`alt_lba` フィールドではなく）。alt_lba が最終 LBA と異なる非標準構成では、プライマリ破損時のカーネル・フォールバックが効かない点に注意。

### 2.2 エントリ（128B × n）

type GUID(16) / partition GUID(16) / start LBA(8) / end LBA(8) / flags(8) / name(72, UTF-16LE)。
GUID は **mixed-endian**（先頭 3 グループのみ LE 格納）なので、raw バイトの先頭 4 バイトをそのまま hex 表示すると canonical 表示と逆に見える（例: ESP の raw `28 73 2a c1 ...` = `c12a7328-...`）。

よく使う type（raw バイト / canonical）:

| 用途 | raw | canonical |
|---|---|---|
| EFI System | `28732ac11ff8d211ba4b00a0c93ec93b` | c12a7328-f81f-11d2-ba4b-00a0c93ec93b |
| Linux RAID | `0f889da1fc053b4da006743f0f84911e` | a19d880f-05fc-4d3b-a006-743f0f84911e（カーネルが RAID 自動アセンブルの対象に認識） |
| Linux fs | `af3dc60f838472478e793d69d8477de4` | 0fc63daf-8483-4772-8e79-3d69d8477de4 |
| BIOS Boot (bios_grub) | `16e3c9e35c0bb84d817df92df00215ae` | e3c9e316-0b5c-4db8-817d-f92df00215ae（標準 BIOS Boot 型。GRUB legacy の core 置き場。2MiB 以上、flags 0x2 (NoBlockIO) が一般的） |

RAID type を持った GPT パーティションは `ADDPART_FLAG_RAID` により**カーネルが md を自動アセンブル**する（mdadm.conf 依存なし、起動数秒内）。関連: 自動アセンブルされたアレイは `MD_AUTO_READ` となり mdstat に `(auto-read-only)` と表示されるが、**初回書き込み BIO で自動的に RW に昇格**する（`drivers/md/md.c` の `md_write_start()`）— 処置不要。

### 2.3 protective MBR

- 0xEE レコードは**単一**（start=1, size=最終-1）か sgdisk 流の**二重**（GPT ヘッダ領域 + 残り）のどちらもあり。カーネルは 1 件あれば譲る。
- sgdisk が protective MBR に `"Hah!IdontNeedEFI"` 文字列を残す（Easter egg。LBA0 の boot code 領域に出たら sgdisk 経由の GPT 作成の痕跡）。
- LBA0 の byte 0-445 は boot code（GRUB stage1 等）領域、510-511 = 55AA。**テーブル領域（446-509）だけの patch** が最小修復（§5 の自動修復パターン）。

## 3. md v1.2 スーパブロック — 「沈黙的 -EINVAL」

### 3.1 レイアウト（バイトオフセット、LE）

```
0:4 magic(=a92b4efc)  4:8 major_version(=1)  8:12 feature_map  12:16 pad0
16:32 set_uuid  32:64 set_name  64:72 ctime  72:76 level  76:80 layout
80:88 size  88:92 chunksize  92:96 raid_disks  96:100 bitmap_offset
100:104 new_level  104:112 reshape_position  112:116 delta_disks  116:120 new_layout
120:124 new_chunk  124:128 new_offset  128:136 data_offset  136:144 data_size
144:152 super_offset  152:160 recovery_offset  160:164 dev_number
164:168 cnt_corrected_read  168:184 device_uuid  184 devflags  185 bblog_shift
186:188 bblog_size  188:192 bblog_offset  192:200 utime  200:208 events
208:216 resync_offset  216:220 sb_csum  220:224 max_dev  224:256 pad3(全ゼロ必須)
256: dev_roles(2B エントリ×max_dev、0xffff=空)
```

v1.2 の `sb_start = 8`（**パーティション先頭からのセクタ数**）。カーネルは `read_disk_sb` で `0 + sb_start = 8` セクタ目から 8 セクタ読む。

### 3.2 検証順と沈黙経路（`super_1_load`）

1. magic / major_version / max_dev / super_offset / feature_map
2. `sb_csum` 不一致 → **ここだけ** `md: invalid superblock checksum on %pg` と警告
3. `data_size < 10` → 警告
4. **`pad0` か `pad3` が非ゼロ → 警告なしで `-EINVAL`（沈黙経路）**
5. data_offset 整合性
6. bad-block ログ関連（feature_map=0 なら無関係）
7. 末尾 `sectors < data_size` チェック

```c
if (sb->pad0 || sb->pad3[0] ||
    memcmp(sb->pad3, sb->pad3+1, sizeof(sb->pad3) - sizeof(sb->pad3[1])))
    return -EINVAL;   /* 警告メッセージなし */
```

**教訓**: csum 対象外の pad 領域の媒体劣化（単一ビット反転すら）で、**カーネルは何も言わずにアセンブルを拒否**する。`-EINVAL` で止まったら警告の有無に関係なく、スーパブロックを raw ダンプして pad フィールドを直接見る。

### 3.3 csum アルゴリズム（`calc_sb_1_csum`）

1. `sb_csum` フィールド（216:220）をゼロ化
2. 先頭 `256 + max_dev*2` バイト（max_dev=128 なら 512B = pad3 と dev_roles 込み）を **LE u32 として 64 ビット加算器**で合計
3. `csum = low32 + high32`（フォールディングは 1 回）
4. 保存値と 32 ビット厳密比較

パッチ例: pad3 を全ゼロ化 → csum 再計算 → 新 csum を 216:220 に書き込む。

### 3.4 オフセットの罠（実事故）

`dd of=/dev/sdX4 bs=512 seek=N` の `seek=N` は**パーティション先頭からの N セクタ**。ディスク絶対セクタをそのまま代入すると、パーティションの奥の**データ領域**に書き込む。さらに読み戻し検証も同じ誤オフセットだと「パッチ済み」という**自己整合的な偽陽性**が何ラウンドも続く（mdadm が古値を報告するのを「キャッシュの遅延」などと誤解釈しやすい）。

- 正しい書き込み: `of=/dev/sdX4 seek=8`（= v1.2 sb_start）
- 検証: 書き込みは `oflag=direct`、読み戻しは `dd iflag=direct | base64` → 送受側でバイト比較
- 最終判定: `mdadm --examine`（カーネルの読込経路 = 別経路）

## 4. GRUB legacy (i386-pc) レイアウト

- **stage1（LBA0 の 512B）**: byte 0-445 = boot code、**ka @90 = core のロードアドレス（通常 0x8000）**、**ks @92 = core の開始 LBA**、510-511 = 55AA
- **core.img の blocklist**: core 内オフセット 500 から（next LBA LE64、len/seg）
- **core の配置**: 正規 = **bios_grub パーティション**（2MiB、GPT type は実機次第、flags 0x2）。「no-BIOS-boot フォールバック」= LBA1 直後に埋め込み（**プライマリ GPT を消費する**）— 一時手段に留めるべきで、恒久状態にしない
- GRUB は UEFI 環境でも GPT を LBA1 の `EFI PART` シグネチャで直接読むため、LBA0 が汚染されても GRUB 自身は /boot を探せることがある（= 「GRUB メニューは出るがカーネル以降で死ぬ」状況になり得る。決定的なゲートはカーネル側）

## 5. NVRAM / EFI 変数のフォレンジクス（非標準ファームウェア）

「UEFI ブートエントリが正しく見えるのに起動しない」場合の手順:

1. **efibootmgr の表示を信用しない**（標準レイアウトでパースするため、非標準ファームウェアのデータでは表示がズレる）
2. **efivarfs の生バイトを読む**（`/sys/firmware/efi/efivars/Boot000X-*`）。注意: 一部ベンダはファイルが**データのみ**（通常の 16B GUID プレフィックス**なし**、byte 0 からデータ開始）
3. `BootCurrentDP`（ファームウェアが実際に使用したデバイスパス）を読む — 正しくブートした経路の**唯一の確実な情報源**
4. HD ノードは標準 EFI Device Path と**フィールド位置・len の意味が異なる**ことがある（len がヘッダ込み総サイズ、PartitionNumber のオフセットが異なる、partGUID がファームウェア生成の偽 GUID、ディスク選択が hdno のみ等）
5. **ファームウェアのディスク番号 (hdno) はデバイス在位状況に依存**する（USB 挿在時にだけエイリアス解決、抜去で全滅、等の事例あり）。標準 GPT HD ノード（sigtype=2 + 実 diskGUID）を解決しないファームウェアが存在する
6. 解決不能なら **legacy BBS ブートデバイス**（ファームウェアが物理ディスクを MBR/LBA0 経由で読む経路）+ GRUB legacy に転換するのが実用的。ただし §1 の LBA0 ゲート問題を引きずる可能性が高い（本ケースの実際の結末）

## 6. 関連するカーネル/GRUB ソースの座標

| 論理 | 出典 |
|---|---|
| 0xEE で GPT パーサに譲る | `block/partitions/msdos.c` (`msdos_partition`) |
| GPT パース + RAID フラグ | `block/partitions/efi.c` (RAID type → `ADDPART_FLAG_RAID`) |
| スーパブロック検証 / 自動アセンブル昇格 | `drivers/md/md.c` (`super_1_load`, `md_write_start`) |
| GRUB stage1 / core 埋め込み | GRUB `grub-core/boot/i386/pc/{diskboot.S,boot.S}`, `grub-core/disk/i386/pc/` |
