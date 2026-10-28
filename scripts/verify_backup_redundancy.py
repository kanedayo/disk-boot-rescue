#!/usr/bin/env python3
"""Verify that a live directory holds everything in a backup tar.

Before deleting a large backup (tens of GiB of tar, ...), prove per file
that the live directory holds it. The comparison is on (size, path):

  missing   path present in tar but absent in live
            -> BLOCKING: this tar may be the last copy of that file
  mismatch  path in both, sizes differ
            -> BLOCKING: check which side is newer before deleting
  extra     path in live but not in tar
            -> NOT blocking (the tar is simply older); `._*` / `.DS_Store`
               are counted separately as macOS noise

DIRECTION RULE: only "in tar, missing in live" blocks deletion. "In live,
not in tar" (extra) never blocks.

Usage:
  python3 verify_backup_redundancy.py backup.tar /path/to/live/dir
  python3 verify_backup_redundancy.py --max-list 100 backup.tar /live/dir

Exit codes:
  0  redundant   (missing == 0 and mismatch == 0) - safe to delete
  1  NOT redundant (missing or mismatch > 0) - do NOT delete
  2  error (tar/dir cannot be read, ...)

Implementation notes:
  * The tar is listed with `LC_ALL=C tar -tvf` so non-ASCII names are
    printed as octal escapes (\\NNN), which we decode back to UTF-8.
  * Both bsdtar (`mode nlink uid gid size mon day hh:mm:ss path`) and GNU
    tar (`mode user/group size mon day hh:mm:ss year path`) line formats
    are auto-detected (field 2 contains '/' only in GNU output).
  * Paths are parsed with a maxsplit so spaces inside filenames survive.
    awk-style field splitting collapses consecutive spaces and produces
    false "missing" files.
  * The live side uses `find -printf` (GNU) with a `find -print0` +
    `stat` fallback (macOS/BSD).
"""

import argparse
import os
import re
import subprocess
import sys

_OCT = re.compile(r'\\([0-7]{3})')
_SENTINEL = '\x00bslash\x00'

# Date part of BSD-family listings:  "Mon dd hh:mm:ss[ yyyy]" (3-4 tokens)
# or ISO "yyyy-mm-dd hh:mm" (2 tokens). Path = the rest (may contain spaces).
_BSD_DATE = re.compile(
    r'^(?:[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}(?::\d{2})?(?:\s+\d{4})?'
    r'|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(?P<path>.+)$')


_OCT_RUN = re.compile(r'(?:\\[0-7]{3})+')


def unesc(s):
    """Decode a bsdtar (LC_ALL=C) octal-escaped path back to UTF-8.

    Consecutive \\NNN escapes are decoded together as one byte run, so a
    UTF-8 multi-byte character (e.g. \\343\\202\\254 = U+65E5) survives.
    """
    s = s.replace('\\\\', _SENTINEL)

    def repl(m):
        raw = b''.join(bytes([int(c, 8)]) for c in _OCT.findall(m.group(0)))
        return raw.decode('utf-8', 'surrogateescape')

    return _OCT_RUN.sub(repl, s).replace(_SENTINEL, '\\')


def parse_tar_line(line):
    """Return (size, path) for a regular-file listing line, else None.

    Auto-detects GNU tar (`mode user/group size mon day hh:mm:ss year path`)
    vs BSD-family (`mode nlink owner group size <date 2-4 tokens> path`).
    Field 2 contains '/' only in GNU output. maxsplit keeps spaces inside
    the path intact (awk-style splitting would collapse them).
    """
    if not line.startswith('-'):
        return None                      # dir/symlink/... lines
    f = line.split(None, 2)
    if len(f) < 3:
        return None
    if '/' in f[1]:                      # GNU tar
        parts = line.split(None, 7)      # size @2, path @7..
        if len(parts) < 8 or not parts[2].isdigit():
            return None
        size, path = parts[2], parts[7]
    else:                                # BSD family
        parts = line.split(None, 5)      # size @4, remainder @5
        if len(parts) < 6 or not parts[4].isdigit():
            return None
        m = _BSD_DATE.match(parts[5])
        if not m:
            return None
        size, path = parts[4], m.group('path')
    path = unesc(path)
    if path.startswith('./'):
        path = path[2:]
    return int(size), path


def tar_listing(tar_path):
    """{path: size} for all regular files in the tar (duplicates -> max)."""
    env = dict(os.environ, LC_ALL='C')
    p = subprocess.run(['tar', '-tvf', tar_path], env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise OSError('tar listing failed: %s'
                      % p.stderr.decode('utf-8', 'replace').strip())
    files = {}
    for line in p.stdout.decode('utf-8', 'surrogateescape').splitlines():
        parsed = parse_tar_line(line)
        if parsed is None:
            continue
        size, path = parsed
        files[path] = max(files.get(path, -1), size)
    return files


def live_listing(root):
    """{relpath: size} for all regular files under `root`."""
    root = os.path.abspath(root)
    files = {}

    if sys.platform != 'darwin':
        # GNU find: -printf '%s %P\n' (%P = path relative to root)
        p = subprocess.run(['find', root, '-type', 'f', '-printf', '%s %P\n'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0:
            for line in p.stdout.decode('utf-8', 'surrogateescape').splitlines():
                size, _, path = line.partition(' ')
                if size.isdigit():
                    files[path] = int(size)
            return files

    # Fallback (macOS/BSD): find -print0 + stat in batches of 1000
    q = subprocess.run(['find', root, '-type', 'f', '-print0'],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if q.returncode != 0:
        raise OSError('find failed: %s'
                      % q.stderr.decode('utf-8', 'replace').strip())
    paths = [x for x in q.stdout.split(b'\x00') if x]
    fmt = '-f' if sys.platform == 'darwin' else '-c'
    pat = '%z %N' if sys.platform == 'darwin' else '%s %n'
    for i in range(0, len(paths), 1000):
        chunk = paths[i:i + 1000]
        sp = subprocess.run(['stat', fmt, pat, *chunk],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if sp.returncode != 0:
            continue
        for line in sp.stdout.decode('utf-8', 'surrogateescape').splitlines():
            size, _, full = line.partition(' ')
            if size.isdigit():
                files[full[len(root) + 1:]] = int(size)
    return files


def is_noise(path):
    base = path.rsplit('/', 1)[-1]
    return base.startswith('._') or base == '.DS_Store'


def capped(items, limit):
    shown = items[:limit]
    note = '' if len(items) <= limit else ' ... (+%d more)' % (len(items) - limit)
    return shown, note


def main():
    ap = argparse.ArgumentParser(
        description='Verify a live dir holds everything in a backup tar')
    ap.add_argument('tar', help='path to the backup tar archive')
    ap.add_argument('live', help='path to the live directory')
    ap.add_argument('--max-list', type=int, default=50,
                    help='max files printed per category (default 50)')
    args = ap.parse_args()

    if not os.path.isfile(args.tar):
        print('ERROR: no such tar: %s' % args.tar)
        return 2
    if not os.path.isdir(args.live):
        print('ERROR: no such directory: %s' % args.live)
        return 2

    try:
        t = tar_listing(args.tar)
        l = live_listing(args.live)
    except OSError as e:
        print('ERROR: %s' % e)
        return 2
    if not t:
        print('ERROR: no regular files found in tar')
        return 2

    missing = sorted(p for p in t if p not in l)
    mismatch = sorted(p for p in t if p in l and t[p] != l[p])
    extra = sorted(p for p in l if p not in t)
    noise = [p for p in extra if is_noise(p)]
    extra_real = [p for p in extra if p not in noise]

    t_total = sum(t.values())
    l_total = sum(l.values())
    miss_bytes = sum(t[p] for p in missing)
    mis_bytes = sum(abs(t[p] - l[p]) for p in mismatch)

    print('tar : %s (%d files, %d bytes)' % (args.tar, len(t), t_total))
    print('live: %s (%d files, %d bytes)' % (args.live, len(l), l_total))
    print()
    print('missing  (in tar, absent in live): %d' % len(missing))
    print('mismatch (size differs)          : %d' % len(mismatch))
    print('extra    (in live, not in tar)   : %d  (%d macOS noise)'
          % (len(extra), len(noise)))

    for label, items, limit in (
            ('missing', missing, args.max_list),
            ('mismatch', mismatch, args.max_list),
            ('extra (non-noise)', extra_real, args.max_list)):
        if not items:
            continue
        print()
        print('--- %s ---' % label)
        shown, note = capped(items, limit)
        for p in shown:
            if label == 'mismatch':
                print('  %s (tar=%d, live=%d)' % (p, t[p], l[p]))
            else:
                print('  %s' % p)
        print(note)
    if mismatch:
        print()
        print('NOTE: for mismatches check which side is newer; a tar-side')
        print('      file newer than the live copy blocks deletion.')

    print()
    print('missing bytes: %d (%.1f MiB)' % (miss_bytes, miss_bytes / 2 ** 20))
    print('mismatch delta: %d bytes' % mis_bytes)

    if not missing and not mismatch:
        print('VERDICT: REDUNDANT - live holds everything in the tar '
              '(exit 0, safe to delete)')
        return 0
    print('VERDICT: NOT REDUNDANT - do NOT delete the tar (exit 1)')
    return 1


if __name__ == '__main__':
    sys.exit(main())
