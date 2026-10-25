#!/bin/sh
# Termux has no /usr/bin/env: the line below is a shell/python polyglot that re-execs
# this file under python3 (in Python it is just a string literal).
''''exec python3 "$0" "$@" #'''
"""
tzsetup.py -- give the container rootfs a real /etc/localtime + /usr/share/zoneinfo.

Android's zoneinfo lives in a packed `tzdata` blob that glibc cannot read, and the
Debian slim bases ship no tzdata, so containers would otherwise log in UTC.
Uses the `tzdata` PyPI wheel (pure data) as the source of TZif files.
"""

import argparse
import os
import shutil
import sys


def zoneinfo_dir():
    try:
        import tzdata  # type: ignore
    except ImportError:
        return None
    path = os.path.join(os.path.dirname(tzdata.__file__), 'zoneinfo')
    return path if os.path.isdir(path) else None


def apply(rootfs, tz, copy_tree=True):
    src_root = zoneinfo_dir()
    if not src_root:
        return 'no tzdata module'
    src = os.path.join(src_root, tz)
    if not os.path.exists(src):
        return 'unknown tz %s' % tz
    etc = os.path.join(rootfs, 'etc')
    os.makedirs(etc, exist_ok=True)
    shutil.copyfile(src, os.path.join(etc, 'localtime'))
    if copy_tree:
        dst_root = os.path.join(rootfs, 'usr', 'share', 'zoneinfo')
        if not os.path.exists(os.path.join(dst_root, 'UTC')):
            shutil.copytree(src_root, dst_root, symlinks=True, dirs_exist_ok=True)
    return 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--tz', required=True)
    ap.add_argument('--no-tree', action='store_true')
    args = ap.parse_args()

    rootfs_base = os.path.join(args.root, 'rootfs')
    if not os.path.isdir(rootfs_base):
        raise SystemExit('error: %s not found' % rootfs_base)
    status = 1
    for name in sorted(os.listdir(rootfs_base)):
        rootfs = os.path.join(rootfs_base, name)
        if not os.path.isdir(rootfs):
            continue
        result = apply(rootfs, args.tz, copy_tree=not args.no_tree)
        print('  %-24s %s' % (name, result))
        if result == 'ok':
            status = 0
        elif result == 'no tzdata module':
            print('  (install it with: pip install tzdata)', file=sys.stderr)
    return status


if __name__ == '__main__':
    sys.exit(main())