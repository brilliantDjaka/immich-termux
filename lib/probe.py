#!/bin/sh
# Termux has no /usr/bin/env: the line below is a shell/python polyglot that re-execs
# this file under python3 (in Python it is just a string literal).
''''exec python3 "$0" "$@" #'''
"""
probe.py -- tiny health probes used by the `immich` CLI (no dependencies).

  probe.py tcp  HOST PORT [--timeout 2]
  probe.py http URL [--expect 200] [--contains TEXT] [--timeout 4]
  probe.py http-any URL [--timeout 4] [--max-status 399]
  probe.py redis HOST PORT [--timeout 2]
  probe.py wait-tcp HOST PORT [--timeout 180] [--interval 1]
"""

import argparse
import socket
import sys
import time
import urllib.error
import urllib.request


def tcp(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return 0
    except OSError:
        return 1


def wait_tcp(host, port, timeout=180.0, interval=1.0):
    deadline = time.time() + timeout
    while True:
        if tcp(host, port, min(2.0, interval + 1)) == 0:
            return 0
        if time.time() >= deadline:
            return 1
        time.sleep(interval)


def redis(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.sendall(b'PING\r\n')
            data = sock.recv(64)
            return 0 if data.startswith(b'+PONG') else 1
    except OSError:
        return 1


def http(url, expect=200, contains=None, timeout=4.0, max_status=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'immich-termux-probe'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read(65536).decode(errors='replace')
    except urllib.error.HTTPError as exc:
        code = exc.code
        body = exc.read(65536).decode(errors='replace')
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 1
    if max_status is not None:
        return 0 if code <= max_status else 1
    if code != expect:
        print('status %s' % code, file=sys.stderr)
        return 1
    if contains and contains not in body:
        print('body does not contain %r' % contains, file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['tcp', 'wait-tcp', 'redis', 'http', 'http-any'])
    ap.add_argument('target')
    ap.add_argument('port', nargs='?')
    ap.add_argument('--timeout', type=float, default=None)
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--expect', type=int, default=200)
    ap.add_argument('--max-status', type=int, default=None)
    ap.add_argument('--contains', default=None)
    args = ap.parse_args()

    if args.mode == 'tcp':
        return tcp(args.target, args.port, args.timeout or 2.0)
    if args.mode == 'wait-tcp':
        return wait_tcp(args.target, args.port, args.timeout or 180.0, args.interval)
    if args.mode == 'redis':
        return redis(args.target, args.port, args.timeout or 2.0)
    if args.mode == 'http':
        return http(
            args.target,
            expect=args.expect,
            contains=args.contains,
            timeout=args.timeout or 4.0,
            max_status=args.max_status,
        )
    return http(args.target, timeout=args.timeout or 4.0, max_status=args.max_status or 399)


if __name__ == '__main__':
    sys.exit(main())