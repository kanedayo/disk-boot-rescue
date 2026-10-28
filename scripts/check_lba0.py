#!/usr/bin/env python3
"""LBA0 (protective MBR) + GPT read-only diagnostic.

Checks the "kernel partition parsing gate" of a disk (or an image of one):

  1. LBA0 partition-table region (446-509). The kernel's msdos parser
     (block/partitions/msdos.c) scans the 4 MBR records in index order and
     yields to the GPT parser only when it reaches a 0xEE protective record
     BEFORE it has registered any valid non-0xEE record. A
     "valid-looking non-protective MBR" (factory 0x01 records, legacy
     residue, hybrid MBR) therefore makes the kernel register only the MBR
     partitions and the GPT is completely ignored: the partitions "do not
     exist" for the kernel and RAID members cannot be assembled. A zeroed
     LBA0 is harmless (msdos yields).

  2. Primary GPT (0-based LBA 1): 'EFI PART' signature, header CRC32
     (bytes 16:20 zeroed), partition-entry-array CRC32, and the entry list.

  3. Backup GPT: read at the primary's alt_lba, falling back to the last
     LBA (which is what the kernel's fallback reads).

Usage:
  sudo python3 check_lba0.py /dev/sda        # block device (root for raw read)
  python3 check_lba0.py disk.img             # image file (no root needed)
  python3 check_lba0.py --json /dev/sda      # machine-readable (for watchdogs)

Exit codes:
  0  OK         gate is safe (0xEE before any valid record, or zeroed LBA0)
                + primary GPT valid
  1  DANGER/WARN kernel hides the GPT (valid non-0xEE record before 0xEE),
                 or one of the two GPT copies is invalid
  2  CRITICAL   both primary and backup GPT invalid
  3  ERROR      device cannot be opened/read

Read-only: opens with O_RDONLY, never writes.
"""

import argparse
import json
import os
import struct
import sys
import zlib

SECTOR = 512

# GPT partition type GUIDs as raw 16 bytes (mixed-endian on disk)
TYPE_ESP = bytes.fromhex('28732ac11ff8d211ba4b00a0c93ec93b')   # c12a7328-...
TYPE_RAID = bytes.fromhex('0f889da1fc053b4da006743f0f84911e')  # a19d880f-...
TYPE_LINFS = bytes.fromhex('af3dc60f838472478e793d69d8477de4')  # 0fc63daf-...
TYPE_NAMES = {TYPE_ESP: 'EFI System', TYPE_RAID: 'Linux RAID',
              TYPE_LINFS: 'Linux fs'}


class Disk:
    """Read-only access to a block device or an image file."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        st = os.fstat(self.fd)
        if os.path.isdir(path):
            self.close()
            raise OSError('%s: is a directory' % path)
        # For block devices st_size is the number of 512-byte sectors.
        self.total_sectors = st.st_size if (st.st_mode & 0o170000) == 0o060000 \
            else st.st_size // SECTOR

    def read(self, lba, length):
        """Read `length` bytes starting at 0-based LBA `lba`."""
        os.lseek(self.fd, lba * SECTOR, os.SEEK_SET)
        return os.read(self.fd, length)

    def close(self):
        os.close(self.fd)


def parse_mbr(lba0, total_sectors):
    """Parse the MBR table region of LBA0 and classify the kernel gate."""
    recs = []
    for i in range(4):
        r = lba0[446 + i * 16:446 + (i + 1) * 16]
        ptype = r[1]
        start, size = struct.unpack_from('<II', r, 5)
        valid = (ptype not in (0, 0xEE) and size > 0
                 and start + size <= total_sectors)
        recs.append({'index': i, 'type': '0x%02x' % ptype, 'start': start,
                     'size': size, 'valid': bool(valid),
                     'protective': bool(ptype == 0xEE)})
    has_sig = lba0[510:512] == b'\x55\xaa'
    zeroed = lba0 == b'\x00' * SECTOR

    first_ee = next((r['index'] for r in recs if r['protective']), None)
    first_non_ee = next((r['index'] for r in recs if r['valid']), None)

    # Kernel behaviour (block/partitions/msdos.c): whichever is seen first
    # in index order wins.
    if first_ee is not None and (first_non_ee is None or first_ee < first_non_ee):
        gate = 'safe-ee'
    elif first_non_ee is not None:
        gate = 'danger-non-protective-mbr'
    else:
        gate = 'safe-no-records'

    return {'has_55aa': bool(has_sig), 'zeroed': bool(zeroed),
            'records': recs, 'first_ee_index': first_ee,
            'first_non_ee_index': first_non_ee, 'kernel_gate': gate}


def parse_gpt(disk, lba):
    """Parse + validate a GPT header and its entry array at 0-based LBA."""
    info = {'lba': lba}
    errs = []
    hdr = disk.read(lba, 92)
    if len(hdr) < 92:
        info['error'] = 'header not readable (beyond end of media?)'
        return info
    if hdr[:8] != b'EFI PART':
        info['error'] = "signature != 'EFI PART' (found %r)" % bytes(hdr[:8])
        return info
    (rev, hsize, h_crc, reserved, my_lba, alt_lba, first_usable, last_usable,
     disk_guid, entry_lba, entry_count, entry_size, a_crc) = \
        struct.unpack_from('<IIIIQQQQ16sQIII', hdr, 8)
    info.update({'my_lba': my_lba, 'alt_lba': alt_lba,
                 'first_usable': first_usable, 'last_usable': last_usable,
                 'disk_guid': disk_guid.hex(), 'entry_lba': entry_lba,
                 'entry_count': entry_count, 'entry_size': entry_size})
    if rev != 0x00010000:
        errs.append('revision 0x%08x != 0x00010000' % rev)
    if my_lba != lba:
        errs.append('my_lba %d != actual position %d' % (my_lba, lba))

    buf = bytearray(hdr)
    buf[16:20] = b'\x00' * 4          # zero the HeaderCRC32 field
    calc = zlib.crc32(bytes(buf)) & 0xFFFFFFFF
    info['hdr_crc_ok'] = bool(calc == h_crc)
    if calc != h_crc:
        errs.append('header CRC mismatch (stored 0x%08x, calc 0x%08x)'
                    % (h_crc, calc))

    a_len = entry_count * entry_size
    arr = disk.read(entry_lba, a_len) if a_len else b''
    if len(arr) < a_len:
        errs.append('entry array extends beyond end of media')
    else:
        calc_a = zlib.crc32(arr) & 0xFFFFFFFF
        info['array_crc_ok'] = bool(calc_a == a_crc)
        if calc_a != a_crc:
            errs.append('entry array CRC mismatch (stored 0x%08x, calc 0x%08x)'
                        % (a_crc, calc_a))
        entries = []
        for i in range(entry_count):
            e = arr[i * entry_size:(i + 1) * entry_size]
            t = e[:16]
            if t == b'\x00' * 16:
                continue
            start, end = struct.unpack_from('<QQ', e, 32)
            flags = struct.unpack_from('<Q', e, 48)[0]
            name = e[56:128].decode('utf-16-le', 'replace').rstrip('\x00')
            entries.append({'num': i + 1, 'type': t.hex(),
                            'type_name': TYPE_NAMES.get(t, 'other'),
                            'start': start, 'end': end, 'flags': flags,
                            'name': name})
        info['entries'] = entries
    info['ok'] = not errs
    info['errors'] = errs
    return info


def verdict(result):
    mbr = result['mbr']
    p_ok = result['primary_gpt'].get('ok', False)
    b_ok = bool(result['backup_gpt'] and result['backup_gpt'].get('ok'))
    if not p_ok and not b_ok:
        reason = 'both primary and backup GPT are invalid'
        if mbr['kernel_gate'] == 'danger-non-protective-mbr':
            reason += '; additionally LBA0 hides the GPT (see MBR section)'
        return 'CRITICAL', 2, reason
    if mbr['kernel_gate'] == 'danger-non-protective-mbr':
        return ('DANGER', 1,
                'LBA0 has a valid non-protective MBR record before any 0xEE '
                'record; the kernel (msdos.c) will claim the MBR and the GPT '
                'will be completely ignored, even if the GPT is valid')
    if p_ok and b_ok:
        v, r = 'OK', 'gate safe + primary GPT valid (+ backup valid)'
        if mbr['zeroed']:
            r += ' [note: LBA0 is zeroed - harmless for the kernel, but no ' \
                 'protective MBR is present]'
        return v, 0, r
    if p_ok:
        return ('WARN', 1,
                'primary GPT valid but the backup GPT is invalid/missing')
    return ('WARN', 1,
            'primary GPT invalid, backup GPT valid - the kernel uses the '
            'backup (no write-back; repair with a GPT tool)')


def print_human(result):
    mbr = result['mbr']
    print('== %s (%d sectors) ==' % (result['device'],
                                     result['total_sectors']))
    print('LBA0:')
    print('  55AA: %s   zeroed: %s' % (mbr['has_55aa'], mbr['zeroed']))
    for r in mbr['records']:
        if r['type'] == '0x00' and not r['protective']:
            continue
        tag = 'protective' if r['protective'] else ('valid' if r['valid']
                                                    else 'empty/invalid')
        print('  [%d] %s start=%d size=%d  (%s)'
              % (r['index'], r['type'], r['start'], r['size'], tag))
    gate = mbr['kernel_gate']
    gate_desc = {
        'safe-ee': '0xEE seen before any valid record -> msdos.c yields to '
                   'the GPT parser (OK)',
        'safe-no-records': 'no valid MBR record -> msdos.c yields (OK)',
        'danger-non-protective-mbr':
            'VALID NON-PROTECTIVE RECORD BEFORE ANY 0xEE -> the kernel '
            'claims the MBR and IGNORES THE GPT',
    }[gate]
    print('  kernel gate: %s' % gate_desc)

    for label in ('primary_gpt', 'backup_gpt'):
        g = result[label]
        if not g:
            print('%s GPT: %s' % (label.split('_')[0].capitalize(),
                                  'not found'))
            continue
        where = ' (LBA %d%s)' % (
            g['lba'], ' = last LBA' if g['lba'] == result['total_sectors'] - 1
            else '')
        if 'error' in g:
            print('%s GPT%s: %s' % (label.split('_')[0].capitalize(), where,
                                    g['error']))
            continue
        print('%s GPT%s: %s' % (label.split('_')[0].capitalize(), where,
                                'OK' if g['ok'] else 'INVALID'))
        for e in g.get('errors', []):
            print('    - %s' % e)
        for e in g.get('entries', []):
            print('    %d: %-11s LBA %d..%d  flags=%d  %s'
                  % (e['num'], e['type_name'], e['start'], e['end'],
                     e['flags'], e['name'] or '(unnamed)'))

    v, code, reason = verdict(result)
    print('VERDICT: %s (exit %d)' % (v, code))
    print('  %s' % reason)


def main():
    ap = argparse.ArgumentParser(
        description='LBA0 + GPT read-only diagnostic (see --help)')
    ap.add_argument('device', help='block device (/dev/sdX) or disk image')
    ap.add_argument('--json', action='store_true',
                    help='emit a JSON object instead of text')
    args = ap.parse_args()

    try:
        disk = Disk(args.device)
    except OSError as e:
        print('ERROR: cannot open %s: %s' % (args.device, e), file=sys.stderr)
        return 3

    result = {'device': args.device, 'total_sectors': disk.total_sectors}
    try:
        lba0 = disk.read(0, SECTOR)
        if len(lba0) < SECTOR:
            print('ERROR: cannot read LBA0 of %s' % args.device,
                  file=sys.stderr)
            return 3
        result['mbr'] = parse_mbr(lba0, disk.total_sectors)

        primary = parse_gpt(disk, 1)
        result['primary_gpt'] = primary

        backup = None
        candidates = []
        if primary.get('alt_lba') is not None:
            candidates.append(primary['alt_lba'])
        last = disk.total_sectors - 1
        if last not in candidates:
            candidates.append(last)
        for c in candidates:
            if 1 < c < disk.total_sectors:
                b = parse_gpt(disk, c)
                if b.get('ok'):
                    backup = b
                    break
        result['backup_gpt'] = backup
        if (primary.get('ok') and backup and
                backup['lba'] != disk.total_sectors - 1):
            result['note'] = ('backup header is not at the last LBA; the '
                              'kernel fallback reads the last LBA, so this '
                              'backup may not be used by the kernel')
    finally:
        disk.close()

    v, code, reason = verdict(result)
    if args.json:
        result['verdict'] = v
        result['exit_code'] = code
        result['reason'] = reason
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_human(result)
    return code


if __name__ == '__main__':
    sys.exit(main())
