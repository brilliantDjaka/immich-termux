#!/bin/sh
# Termux has no /usr/bin/env: the line below is a shell/python polyglot that re-execs
# this file under python3 (in Python it is just a string literal).
''''exec python3 "$0" "$@" #'''
"""
svc.py -- run one docker-compose service from its OCI rootfs under proot.

This is the "container runtime" replacement: it reads run/<service>.json (produced by
compose2plan.py), merges the image config (Entrypoint/Cmd/Env/WorkingDir/User) that
oci_pull.py stored in rootfs/.immich-image.json, translates compose volumes into proot
binds, emulates container-name DNS via /etc/hosts, and execs proot.

Commands
  argv <svc>              print the proot argv (debugging)
  env  <svc>              print the resolved environment
  start <svc>             start detached, return immediately
  stop <svc> [--timeout]  SIGTERM the process group, then SIGKILL
  status <svc>            JSON status of the service
  shell <svc>             interactive shell inside the rootfs
  exec <svc> -- cmd ...   run a command inside the rootfs (foreground)
  prepare <svc>           only (re)generate /etc/hosts, resolv.conf, pg override
"""

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

CONF_KEYS = {
    'WITH_ML',
    'ARCH',
    'DNS_SERVERS',
    'NODE_HEAP_MB',
    'PROOT_NO_SECCOMP',
    'PROOT_BIN',
    'IMMICH_BIND_HOST',
    'TZ',
    'PG_LOW_MEM',
    'PG_LOG_STATEMENT',
    'PG_PRELOAD',
    'SHM_COMPAT',
    'RUST_BACKTRACE',
    'ML_URL',
    'LOG_LEVEL',
}


# ----------------------------------------------------------------------------- config


def load_conf(root):
    conf = {}
    path = os.path.join(root, 'immich.conf')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('export '):
                    line = line[7:]
                key, sep, val = line.partition('=')
                if not sep:
                    continue
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
                    val = val[1:-1]
                conf[key.strip()] = val
    for key in CONF_KEYS:
        if key in os.environ:
            conf[key] = os.environ[key]
    return conf


def conf_get(conf, key, default=None):
    val = conf.get(key)
    return default if val is None or val == '' else val


def truthy(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def load_spec(root, name):
    path = os.path.join(root, 'run', name + '.json')
    if not os.path.exists(path):
        raise SystemExit('error: unknown service %r (no %s)' % (name, path))
    with open(path, 'r', encoding='utf-8') as fh:
        spec = json.load(fh)
    image_meta_path = os.path.join(spec['rootfs'], '.immich-image.json')
    image_cfg = {}
    if os.path.exists(image_meta_path):
        with open(image_meta_path, 'r', encoding='utf-8') as fh:
            meta = json.load(fh)
        image_cfg = meta.get('config') or {}
        spec['image_meta'] = {'tag': meta.get('tag'), 'digest': meta.get('config_digest'), 'ref': meta.get('image')}
    else:
        spec['image_meta'] = {}
    spec['image_config'] = image_cfg
    return spec


# ---------------------------------------------------------------------------- helpers


def passwd_lookup(rootfs, names):
    path = os.path.join(rootfs, 'etc', 'passwd')
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        entries = {}
        for line in fh:
            parts = line.strip().split(':')
            if len(parts) >= 4:
                entries[parts[0]] = (int(parts[2]), int(parts[3]))
    for name in names:
        if name in entries:
            return entries[name]
    return None


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return shlex.split(str(value))


def resolve_env(spec, conf):
    """docker semantics: image ENV < compose environment < runtime overrides."""
    env = {}
    for item in spec['image_config'].get('Env') or []:
        key, _, val = str(item).partition('=')
        env[key] = val
    env.update({str(k): str(v) for k, v in (spec.get('env') or {}).items()})

    role = spec['role']
    bind_host = conf_get(conf, 'IMMICH_BIND_HOST', '0.0.0.0')
    container_name = spec.get('container_name') or spec['name']

    env.setdefault('HOME', '/root')
    env['HOSTNAME'] = container_name
    if spec.get('rootfs'):
        env.setdefault('TERM', 'xterm-256color')

    if 'TZ' in (spec.get('env') or {}):
        env['TZ'] = str(spec['env']['TZ'])
    elif conf_get(conf, 'TZ'):
        env['TZ'] = str(conf_get(conf, 'TZ'))

    for port in spec.get('ports') or []:
        if port.get('host') and port.get('container') and port['host'] != port['container']:
            if role in ('server', 'ml'):
                # these honour IMMICH_PORT, so a remapped host port stays faithful
                env['IMMICH_PORT'] = str(port['host'])
    if role in ('server', 'ml'):
        env['IMMICH_HOST'] = bind_host
        env.setdefault('IMMICH_PORT', '2283' if role == 'server' else '3003')
    if role == 'server':
        heap = conf_get(conf, 'NODE_HEAP_MB')
        if heap:
            opts = env.get('NODE_OPTIONS', '')
            env['NODE_OPTIONS'] = (opts + ' --max-old-space-size=%s' % heap).strip()
        env.setdefault('IMMICH_MACHINE_LEARNING_URL', conf_get(conf, 'ML_URL', 'http://immich-machine-learning:3003'))
    if role == 'database':
        # SysV shared memory is emulated by shm/shm_compat.c, activated through the
        # rootfs's /etc/ld.so.preload (see install_shm_compat).
        env.setdefault('SHM_COMPAT_DIR', '/dev/shm/sysv')
        if conf_get(conf, 'RUST_BACKTRACE'):
            env['RUST_BACKTRACE'] = str(conf_get(conf, 'RUST_BACKTRACE'))
    return env


def write_guest_env(root, name, env):
    """Persist the container environment (run/<name>.env) for debugging/inspection."""
    path = os.path.join(root, 'run', name + '.env')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ['%s=%s' % (key, value) for key, value in sorted(env.items()) if '\n' not in str(value)]
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return path


def resolve_fake_id(spec, conf):
    user_hint = spec.get('user')
    if user_hint:
        user_name = str(user_hint).split(':')[0]
        found = passwd_lookup(spec['rootfs'], [user_name])
        if found:
            return found
    names = spec.get('run_as_user') or []
    if names:
        found = passwd_lookup(spec['rootfs'], names)
        if found:
            return found
    return None  # -> proot -0


def shm_compat_lib(root):
    """Path to the SysV shared memory shim that gets LD_PRELOADed into PostgreSQL.

    Android's seccomp policy kills shmget(2) with SIGSYS, and PostgreSQL reserves a
    56-byte SysV block even in shared_memory_type=mmap mode. shm/shm_compat.c provides
    the missing calls over MAP_SHARED files (same approach as Termux's libandroid-shmem).
    """
    candidates = [
        os.path.join(root, 'shm', 'libshm-compat.so'),
        os.path.join(root, 'lib', 'prebuilt', 'arm64', 'libshm-compat.so'),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def install_shm_compat(spec, conf, root):
    """Make SysV shared memory available to PostgreSQL on Android.

    The shim is activated through the guest's /etc/ld.so.preload rather than LD_PRELOAD:
    proot itself must not see a preload (termux-exec's shim breaks its tracees), and
    ld.so.preload reliably covers initdb, postgres and every process they exec.
    """
    preload_path = os.path.join(spec['rootfs'], 'etc', 'ld.so.preload')
    if spec['role'] != 'database' or not truthy(conf_get(conf, 'SHM_COMPAT', '1')):
        return None
    source = shm_compat_lib(root)
    if not source:
        print(
            'warn: SysV shared memory shim missing; PostgreSQL cannot start on Android.\n'
            '      build it with:\n'
            '      clang -shared -fPIC -nostdlib -nodefaultlibs -nostartfiles -O2 \\\n'
            '            -Wl,-soname,libshm-compat.so -o shm/libshm-compat.so shm/shm_compat.c',
            file=sys.stderr,
        )
        return None
    dest_rel = 'usr/local/lib/libshm-compat.so'
    dest = os.path.join(spec['rootfs'], dest_rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest) or open(source, 'rb').read() != open(dest, 'rb').read():
        shutil.copyfile(source, dest)
        os.chmod(dest, 0o755)
    want = '/' + dest_rel + '\n'
    current = None
    if os.path.exists(preload_path):
        with open(preload_path, 'r', encoding='utf-8', errors='replace') as fh:
            current = fh.read()
    if current != want:
        os.makedirs(os.path.dirname(preload_path), exist_ok=True)
        with open(preload_path, 'w', encoding='utf-8') as fh:
            fh.write(want)
    return '/' + dest_rel


def pg_override_notes(spec, conf):
    """Generate /etc/postgresql/postgresql.conf with the Android-required settings.

    Two things must be changed from the immich template:

    * ``shared_memory_type = mmap`` -- Android kernels ship without CONFIG_SYSVIPC and
      the app seccomp policy kills ``shmget(2)`` with SIGSYS; ``mmap`` is the supported
      alternative (the 56-byte SysV block PostgreSQL still reserves is emulated by
      shm/shm_compat.c).
    * much smaller memory settings -- a phone has far less RAM than the 512MB
      shared_buffers the template assumes.

    Writing the file ourselves works because immich's entrypoint copies the template
    with ``cp -n`` (no clobber), so an existing /etc/postgresql/postgresql.conf wins.
    The template's relative ``include_if_exists 'postgresql.override.conf'`` cannot be
    used: PostgreSQL 14 resolves it against the data directory, not /etc/postgresql.
    """
    if spec['role'] != 'database':
        return None
    rootfs = spec['rootfs']
    etc_dir = os.path.join(rootfs, 'etc', 'postgresql')
    dest = os.path.join(etc_dir, 'postgresql.conf')

    storage = str((spec.get('env') or {}).get('DB_STORAGE_TYPE') or 'SSD').lower()
    if storage not in ('ssd', 'hdd'):
        storage = 'ssd'
    template = os.path.join(rootfs, 'var', 'postgresql-conf-tpl', 'postgresql.%s.conf' % storage)

    lines = [
        '',
        '# ---- appended by immich-termux (Android/Termux) ----',
        '# SysV shared memory is unavailable here; mmap works and the remaining SysV',
        '# block is emulated by libshm-compat.so (see shm/shm_compat.c).',
        'shared_memory_type = mmap',
        'huge_pages = off',
    ]
    if truthy(conf_get(conf, 'PG_LOW_MEM', '1')):
        lines += [
            '# low-RAM device tuning (set PG_LOW_MEM=0 to keep the immich defaults)',
            'shared_buffers = 192MB',
            'max_wal_size = 1GB',
            'effective_cache_size = 512MB',
            'work_mem = 8MB',
            'maintenance_work_mem = 64MB',
            'max_connections = 40',
        ]
    if conf_get(conf, 'PG_LOG_STATEMENT'):
        lines += ['log_statement = %s' % conf_get(conf, 'PG_LOG_STATEMENT')]
    body = '\n'.join(lines) + '\n'

    if os.path.exists(template):
        with open(template, 'r', encoding='utf-8', errors='replace') as fh:
            base = fh.read()
        # The image preloads vchord.so *and* pgvecto.rs (vectors.so). Immich v3 prefers
        # VectorChord, and pgvecto.rs's IPC panics with EACCES on Android on every
        # transaction hook, so only the extension immich actually uses stays preloaded.
        preload = conf_get(conf, 'PG_PRELOAD')
        if preload is None:
            preload = 'vchord.so'
        rewritten = []
        for line in base.splitlines():
            if line.strip().startswith('shared_preload_libraries'):
                rewritten.append("shared_preload_libraries = '%s'" % preload)
            else:
                rewritten.append(line)
        base = '\n'.join(rewritten) + '\n'
        text = base.rstrip('\n') + '\n' + body
    else:
        # unknown image layout: fall back to the file the template includes last
        text = None
        dest = os.path.join(etc_dir, 'postgresql.override.conf')
        body = '\n'.join(line for line in lines if not line.startswith('# ----')) + '\n'

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    previous = None
    if os.path.exists(dest):
        with open(dest, 'r', encoding='utf-8', errors='replace') as fh:
            previous = fh.read()
    if previous != (text if text is not None else body):
        with open(dest, 'w', encoding='utf-8') as fh:
            fh.write(text if text is not None else body)

    # drop files an earlier version of this project used to write
    stale = os.path.join(etc_dir, 'postgresql.override.conf')
    if os.path.exists(stale):
        try:
            with open(stale, 'r', encoding='utf-8', errors='replace') as fh:
                if 'immich-termux' in fh.read():
                    os.unlink(stale)
        except OSError:
            pass
    pgdata = (spec.get('env') or {}).get('PGDATA', '/var/lib/postgresql/data')
    for bind in spec.get('volumes') or []:
        if bind['dst'] == pgdata:
            leftover = os.path.join(bind['src'], 'postgresql.override.conf')
            if os.path.exists(leftover):
                os.unlink(leftover)
    return dest


def write_hosts(spec):
    rootfs = spec['rootfs']
    aliases = [a for a in (spec.get('hosts_aliases') or []) if a]
    lines = [
        '127.0.0.1 localhost',
        '::1 localhost ip6-localhost ip6-loopback',
        '127.0.0.1 %s' % ' '.join(aliases) if aliases else '',
    ]
    body = '\n'.join(line for line in lines if line) + '\n'
    path = os.path.join(rootfs, 'etc', 'hosts')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current = None
    if os.path.exists(path):
        try:
            current = open(path, 'r', encoding='utf-8', errors='replace').read()
        except OSError:
            current = None
    if current != body:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(body)
    return path


def write_resolv(root, conf):
    servers = str(conf_get(conf, 'DNS_SERVERS', '1.1.1.1 8.8.8.8 9.9.9.9')).replace(',', ' ').split()
    path = os.path.join(root, 'run', 'resolv.conf')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = '\n'.join('nameserver %s' % s for s in servers)
    body += '\noptions timeout:2 attempts:2\n'
    current = open(path, 'r', encoding='utf-8').read() if os.path.exists(path) else None
    if current != body:
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(body)
    return path


def ensure_bind_target(rootfs, dst):
    """proot requires bind targets to exist inside the rootfs."""
    full = os.path.join(rootfs, dst.lstrip('/'))
    if os.path.isdir(full) or os.path.isfile(full):
        return
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if os.path.splitext(dst)[1] and not dst.endswith('/'):
        open(full, 'a').close()
    else:
        os.makedirs(full, exist_ok=True)


def build(root, spec, conf):
    rootfs = spec['rootfs']
    if not os.path.isdir(rootfs):
        raise SystemExit('error: rootfs missing for %s (%s) -- run `immich pull`' % (spec['name'], rootfs))

    write_hosts(spec)
    resolv = write_resolv(root, conf)
    pg_override_notes(spec, conf)
    install_shm_compat(spec, conf, root)
    workdir = spec.get('workdir') or spec['image_config'].get('WorkingDir') or '/'
    workdir = workdir.strip() or '/'
    if not os.path.isdir(os.path.join(rootfs, workdir.lstrip('/'))):
        workdir = '/'

    binds = []
    for path in ('/dev', '/proc', '/sys'):
        if os.path.exists(path):
            binds.append((path, None))

    shm = os.path.join(root, 'volumes', 'shm')
    os.makedirs(shm, exist_ok=True)
    binds.append((shm, '/dev/shm'))
    binds.append((resolv, '/etc/resolv.conf'))

    for bind in spec.get('volumes') or []:
        src = bind['src']
        if not os.path.exists(src):
            # docker creates missing bind sources; we do too for paths that belong to the
            # compose project, but an absolute host path (e.g. /etc/localtime) is just
            # skipped because Android does not have it.
            if bind.get('origin') == 'absolute':
                print('warn: host path %s does not exist, skipping bind for %s' % (src, bind['dst']), file=sys.stderr)
                continue
            os.makedirs(src, exist_ok=True)
        binds.append((src, bind['dst']))

    if spec['role'] == 'redis' and not any(b['dst'] == '/data' for b in spec.get('volumes') or []):
        data = os.path.join(root, 'volumes', 'redis-data')
        os.makedirs(data, exist_ok=True)
        binds.append((data, '/data'))

    for _, dst in binds:
        if dst:
            ensure_bind_target(rootfs, dst)

    argv = [conf_get(conf, 'PROOT_BIN', 'proot'), '--kill-on-exit', '-r', rootfs, '-w', workdir]

    fake_id = resolve_fake_id(spec, conf)
    if fake_id:
        argv += ['-i', '%d:%d' % fake_id]
    else:
        argv.append('-0')

    for src, dst in binds:
        argv += ['-b', src if dst is None else '%s:%s' % (src, dst)]

    entrypoint = as_list(spec.get('entrypoint')) or as_list(spec['image_config'].get('Entrypoint'))
    command = as_list(spec.get('command'))
    if not command:
        command = as_list(spec['image_config'].get('Cmd'))
    if spec.get('entrypoint') is not None and spec.get('command') is None:
        command = []

    env = resolve_env(spec, conf)
    write_guest_env(root, spec['name'], env)
    argv += entrypoint + command

    proot_env = dict(os.environ)
    # termux-exec's LD_PRELOAD shim rewrites execve() calls and makes proot's tracees
    # fail with ENOSYS ("Function not implemented"), so proot must see a clean preload.
    proot_env.pop('LD_PRELOAD', None)
    proot_env.update({k: v for k, v in env.items()})
    os.makedirs(os.path.join(root, 'tmp'), exist_ok=True)
    proot_env['PROOT_TMP_DIR'] = os.path.join(root, 'tmp')
    if truthy(conf_get(conf, 'PROOT_NO_SECCOMP', '')):
        proot_env['PROOT_NO_SECCOMP'] = '1'
    if conf_get(conf, 'TZ'):
        proot_env['TZ'] = str(conf_get(conf, 'TZ'))
    return argv, proot_env, fake_id


# ---------------------------------------------------------------------------- process


def pidfile(root, name):
    return os.path.join(root, 'run', name + '.pid')


def clear_shm(root):
    """Empty volumes/shm before a cold start.

    A container always gets a fresh tmpfs on /dev/shm; here it is a directory that
    survives restarts, and PostgreSQL keeps its dynamic shared memory control segment
    there (dynamic_shared_memory_type=posix). Reusing a stale control segment from a
    previous postmaster can crash backends, so it is wiped whenever nothing is running.
    """
    shm = os.path.join(root, 'volumes', 'shm')
    if not os.path.isdir(shm):
        os.makedirs(shm, exist_ok=True)
        return 0
    removed = 0
    for entry in os.listdir(shm):
        path = os.path.join(shm, entry)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def orphan_pids(spec):
    """Processes still running from this service's rootfs (left behind by a crash)."""
    rootfs = spec['rootfs']
    own = os.getpid()
    found = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own:
            continue
        try:
            with open('/proc/%d/cmdline' % pid, 'rb') as fh:
                cmdline = fh.read().decode(errors='replace')
        except OSError:
            continue
        if rootfs in cmdline:
            found.append(pid)
    return found


def kill_orphans(spec, timeout=5.0):
    pids = orphan_pids(spec)
    if not pids:
        return []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline and any(os.path.isdir('/proc/%d' % p) for p in pids):
        time.sleep(0.2)
    for pid in pids:
        if os.path.isdir('/proc/%d' % pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return pids


def logfile(root, name):
    return os.path.join(root, 'logs', name + '.log')


def read_pid(root, name):
    path = pidfile(root, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return None
    pid = data.get('pid')
    if not pid or not os.path.isdir('/proc/%d' % pid):
        return None
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as fh:
            cmdline = fh.read().decode(errors='replace')
    except OSError:
        return None
    if data.get('rootfs') and data['rootfs'] not in cmdline:
        return None
    try:
        with open('/proc/%d/stat' % pid, 'r') as fh:
            state = fh.read().rsplit(')', 1)[1].split()[0]
        if state == 'Z':
            return None
    except OSError:
        return None
    data['alive'] = True
    return data


def rotate_log(path, max_mb=None):
    """Keep logs/<service>.log from growing without bound (one generation is kept)."""
    if max_mb is None:
        try:
            max_mb = float(os.environ.get('LOG_MAX_MB') or 20)
        except ValueError:
            max_mb = 20
    if max_mb <= 0 or not os.path.exists(path):
        return False
    try:
        if os.path.getsize(path) <= max_mb * 1024 * 1024:
            return False
    except OSError:
        return False
    try:
        os.replace(path, path + '.1')
        return True
    except OSError:
        return False


def cmd_start(root, name, spec, conf, wait_start=2.0):
    existing = read_pid(root, name)
    if existing:
        print('%s: already running (pid %d)' % (name, existing['pid']))
        return 0
    argv, env, fake_id = build(root, spec, conf)
    os.makedirs(os.path.join(root, 'logs'), exist_ok=True)
    os.makedirs(os.path.join(root, 'run'), exist_ok=True)
    logpath = logfile(root, name)
    if rotate_log(logpath):
        print('%s: rotated log to %s.1' % (name, os.path.basename(logpath)))
    with open(logpath, 'ab') as logfh:
        logfh.write(('\n=== %s  start %s  cmd=%s ===\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), name, ' '.join(argv))).encode())
        logfh.flush()
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=logfh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            # cwd inside the rootfs keeps proot from warning that the host cwd has no
            # counterpart in the guest; -w sets the real working directory.
            cwd=spec['rootfs'] if os.path.isdir(spec['rootfs']) else root,
        )
    with open(pidfile(root, name), 'w', encoding='utf-8') as fh:
        json.dump(
            {
                'pid': proc.pid,
                'name': name,
                'rootfs': spec['rootfs'],
                'argv': argv,
                'started': int(time.time()),
                'fake_id': fake_id,
                'log': logpath,
            },
            fh,
        )
    if wait_start:
        time.sleep(wait_start)
        if proc.poll() is not None:
            print('%s: exited immediately (code %s) -- last log lines:' % (name, proc.returncode))
            tail(logpath, 40)
            path = pidfile(root, name)
            if os.path.exists(path):
                os.unlink(path)
            return 1
    print('%s: started (pid %d)' % (name, proc.pid))
    return 0


def tail(path, lines=40):
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 65536)
            fh.seek(size - block)
            data = fh.read().decode(errors='replace').splitlines()
    except OSError:
        return
    for line in data[-lines:]:
        print('    ' + line)


def cmd_stop(root, name, timeout=20.0, spec=None):
    data = read_pid(root, name)
    if not data:
        if spec:
            leftovers = kill_orphans(spec)
            if leftovers:
                print('%s: not running, cleaned up %d orphan process(es)' % (name, len(leftovers)))
                path = pidfile(root, name)
                if os.path.exists(path):
                    os.unlink(path)
                return 0
        print('%s: not running' % name)
        path = pidfile(root, name)
        if os.path.exists(path):
            os.unlink(path)
        return 0
    pid = data['pid']
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.isdir('/proc/%d' % pid):
            break
        time.sleep(0.25)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.5)
    path = pidfile(root, name)
    if os.path.exists(path):
        os.unlink(path)
    print('%s: stopped' % name)
    return 0


def cmd_status(root, name):
    data = read_pid(root, name)
    if not data:
        print(json.dumps({'name': name, 'running': False}))
        return 1
    uptime = int(time.time()) - int(data.get('started') or time.time())
    print(json.dumps({'name': name, 'running': True, 'pid': data['pid'], 'uptime_s': uptime, 'log': data.get('log')}))
    return 0


def cmd_exec(root, name, spec, conf, command):
    argv, env, _ = build(root, spec, conf)
    full = _proot_prefix(argv) + as_list(command)
    return subprocess.call(full, env=env, cwd=spec['rootfs'] if os.path.isdir(spec['rootfs']) else root)


def _proot_prefix(argv):
    """Return only the proot options from a built argv (drops entrypoint/cmd)."""
    out = [argv[0]]
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in ('-r', '-w', '-b', '-i'):
            out += [token, argv[i + 1]]
            i += 2
            continue
        if token in ('-0', '--kill-on-exit'):
            out.append(token)
            i += 1
            continue
        break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['argv', 'env', 'start', 'stop', 'status', 'shell', 'exec', 'prepare', 'clear-shm'])
    ap.add_argument('service', nargs='?')
    ap.add_argument('rest', nargs=argparse.REMAINDER)
    ap.add_argument('--root', default=os.environ.get('IMMICH_TERMUX_ROOT') or os.getcwd())
    ap.add_argument('--timeout', type=float, default=20.0)
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    conf = load_conf(root)

    if args.command == 'clear-shm':
        print(clear_shm(root))
        return 0

    if not args.service:
        raise SystemExit('error: %s needs a service name' % args.command)
    spec = load_spec(root, args.service)

    if args.command == 'argv':
        argv, _, fake_id = build(root, spec, conf)
        print('fake_id: %s' % (fake_id,))
        print(shlex.join(argv))
        return 0
    if args.command == 'env':
        env = resolve_env(spec, conf)
        for key in sorted(env):
            print('%s=%s' % (key, env[key]))
        return 0
    if args.command == 'prepare':
        build(root, spec, conf)
        print('%s: prepared' % args.service)
        return 0
    if args.command == 'start':
        return cmd_start(root, args.service, spec, conf)
    if args.command == 'stop':
        return cmd_stop(root, args.service, args.timeout, spec)
    if args.command == 'status':
        return cmd_status(root, args.service)
    if args.command == 'shell':
        shell = '/bin/bash' if os.path.exists(os.path.join(spec['rootfs'], 'bin/bash')) else '/bin/sh'
        return cmd_exec(root, args.service, spec, conf, ['-i', shell])
    if args.command == 'exec':
        rest = list(args.rest)
        if rest and rest[0] == '--':
            rest = rest[1:]
        if not rest:
            raise SystemExit('error: exec needs a command')
        return cmd_exec(root, args.service, spec, conf, rest)
    return 1


if __name__ == '__main__':
    sys.exit(main())