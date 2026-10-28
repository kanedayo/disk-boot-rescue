# PXE 救援ブリッジ（ホスト死、LAN 到達可能）

ホスト本体は起動不能だが LAN で到達可能（BIOS/ファームウェアは動く）場合、救援マシンから **PXE ブリッジ** で救援 initrd を読み込ませ、SSH 経由で診断・修復する手法。

**原則: ブリッジは既定オフ・要求時のみ再構築（30-60 分）。** 常駐させない。常駐 = 追加の攻撃面 + 設定残骸。

## 1. アーキテクチャと構成部品

```
[救援マシンの NIC] ──(192.0.2.x/24 等、RFC 5737 文書用プレースホルダ)── [死んでいるホスト]
   dnsmasq: DHCP(+PXE) + TFTP
   tftp-secure: vmlinuz / initrd.img / 救援スクリプト一式
   sshd(救援側): root から作業
```

- DHCP: PXE クライアント（vendor class `PXEClient`）にだけ救援 IP を出す。**tagged real-IP range** を使う（後述 §2）
- TFTP: `vmlinuz` + `initrd.img`。initrd には `init`（シェル環境 + 救援スクリプト一式）を含める
- **macOS のローカルブロードキャストはローカル側 NIC に再配信されない** → dnsmasq が「応答した」と見えてもホストには届いていない可能性がある。検証は**必ず tcpdump（救援側 NIC）** で DHCPDISCOVER/OFFER/REQUEST/ACK と TFTP の実転送を確認する
- tftp-secure（macOS）は転送ファイルが **`nobody:wheel`** を必要とする。この設定を忘れると 403 が沈黙する
- 対象の PXE は **legacy UNDI**（client-arch 0）であることが多い。UEFI PXE（client-arch 7）とでは dnsmasq の `pxe-service` / `dhcp-boot` の扱いが違う。まず tcpdump の DHCPDISCOVER vendor class / option 93 を見て arch を確定する

## 2. dnsmasq の「沈黙する」原因 2 つ（実測）

`enable_pxe` を書いただけで何も起きない、という報告の大半はこれら:

1. **`enable_pxe=yes` 単独では無効。** `pxe-service=` / `pxe-menu=` のいずれかが 1 件でも定義されていなければ、PXE オプション（66/67 等）を**一切送らない**（dnsmasq の `option.c` 挙動）。
2. **vendor class は option 60、option 61 ではない。** `tag:pxe` 等を vendor class 条件で組む際に 61 で書いてしまうと条件が永遠に成立せず、tagged range が誰も取得しない。
3.（附随）**`opt66` を送らない構成**では siaddr が使えないため、TFTP サーバ IP を `dhcp-boot=file,server`（コンマ形式）で明示する。

動作する最小構成（IP は環境に置き換える）:

```
interface=br0
dhcp-range=192.0.2.200,proxy            # プロキシ: 全 client への応答はしない
dhcp-range=tag:pxe,192.0.2.200,192.0.2.210,255.255.255.0,12h   # PXE のみ
enable-filter=0
pxe-service=x86PC, "rescue", vmlinuz
pxe-menu=x86PC, "rescue"
dhcp-boot=initrd.img,192.0.2.10
tftp-root=/tftp
```

- `tag:pxe`（dnsmasq が option 60 の `PXEClient` から自動付与）で、救援用 IP ブロックが救援 client にだけ見える
- 常駐 DHCP サーバが既に LAN にあるなら、`proxy` + tagged range の 2 段が正解（常駐 DHCP が普通 client を拾い、PXE のみ dnsmasq が応答）

## 3. initrd の手組みレシピ

`update-initramfs`（docker 内で実行）が**壊れた initrd** を生成した実例あり（init 実行不能）。手組みが確実:

```
debian:trixie イメージで:
  apt install linux-image-amd64 initramfs-tools klibc-utils kmod busybox mdadm cpio gzip
```

ツリー:

```
rescue/
  init                 # 救援入口（シェル環境構築 + スクリプト展開 + 救援シェル起動）
  bin/  sbin/  usr/bin/  ...   # 必要バイナリをコピー（busybox, mdadm, sshd 等）
  lib/  lib/modules/<ver>/      # 対象カーネルのモジュール（ata, md, 必要な fs）
  lib/firmware/                # 対象 NIC のファームウェア
```

```
find . | cpio -o -H newc | gzip -9 > initrd.img
```

**成果物の検証**（壊れた initrd を防ぐ）:

- gzip magic: `file initrd.img` = gzip
- cpio: `cpio -itv` で `TRAILER!!!` が最後に存在
- 必須ファイルが揃っているか（`init`, カーネルモジュール、`/sbin/sshd` 等）
- **可能なら QEMU 等で一度起動して検証**（後述 §5 の合成ディスクテストと同じ精神）

## 4. 救援スクリプトの設計（init 内）

- 救援 shell は root で、`/dev` を再構成してから開始
- 診断系（faildiag / bootdiag 等）は **自動アップロード**（TFTP/HTTP PUT 等）する設計にする — 死んだホストからログを逃がさない
- 修復系（repair）は §5 の expect/patch パターン
- 終了は明示: `reboot -f`（BIOS に戻す）。救援 initrd のまま滞留しない

## 5. 自動修復スクリプトの設計パターン（expect/patch + 分類 FATAL）

実障害で 1 PXE サイクルで完結した設計:

1. **pre-state 期待値（expect）**: 修復前にディスクの該当領域を raw 読み、期待パターンと完全一致を確認。一致しなければ**書かない**（理由を分類・記録）
2. **patch**: 期待値を満たすことだけを条件に書込
3. **post-state 期待値 + readback**: 書込後 `O_DIRECT` で読み戻し、期待パターンとバイト一致
4. **FATAL 分類**:

| 分類 | 意味 | 挙動 |
|---|---|---|
| stage1 damaged | ブートローダ領域が壊れている | 書込せず FATAL（手動介入が必要） |
| p5 identical on both disks | 両ディスク同一 = 意図的データかもしれない | 書込せず FATAL（データと区別できない） |
| core lost | 修復対象の領域が欠損 | 書込せず FATAL |
| verify mismatch | readback が不一致 | 書込せず FATAL（**物理エラー疑い** = ケーブル/HDD） |

5. **冪等**: 2 回実行しても 2 回目は pre-state が「すでに修復済み」で安全に無操作
6. 修復後の再起動: `sleep 10; reboot -f`（ログの転送待ち + 即再読込）

### 合成ディスクによるテスト（最重要の教訓）

実ディスクに触る前に、**sparse イメージで同一レイアウト + 同一障害を再現**した合成ディスクで、修復スクリプトを回し **FATAL 全分岐を含む回帰テスト**を通す。本ケースでは 43 チェックのスイートを合成ディスク上で構築し、実障害の再現（T8H 相当）も含めて 43/43 を確認してから実適用した。

```
合成ディスク:  truncate -s <size> d.img
障害注入:      LBA0 に 0xEE のない「有効な」MBR テーブルを書き込む等
検証:          カーネル（QEMU 起動 or losetup）で /proc/partitions を確認
```

## 6. ブリッジの片付け（既定オフにする）

- dnsmasq / tftp-secure / sshd（救援用）を停止
- 救援用 IP 範囲が他の機器と衝突しないか確認
- 残骸（tftp-root, 救援ツリー）は要否を判定: 再構築手順を文書化しているなら削除してよい（再構築 30-60 分）
- **唯一のコピーを /tmp 等に置かない**（再起動で消える）— 必要なら文書的な場所へ移動
