#!/bin/sh
# Termux has no /usr/bin/env: the line below is a shell/python polyglot that re-execs
# this file under python3 (in Python it is just a string literal).
''''exec python3 "$0" "$@" #'''
"""
compose2plan.py -- translate a docker-compose.yaml into a Termux/proot runtime plan.

There is no Docker on Android: unprivileged namespaces are unavailable, so containers
cannot be created. What *is* available is `proot` (ptrace based, no namespaces) which
can run a glibc OCI rootfs. proot shares the host network namespace, so every service
listens on real localhost ports -- exactly the topology docker-compose gives you with
bridge networking. Container-name DNS is emulated with /etc/hosts aliases.

Reads : docker-compose.yaml (the subset of the Compose Spec used by Immich)
Writes: <root>/plan.json          full plan (debugging / other tooling)
        <root>/run/<svc>.json     runtime spec consumed by lib/svc.py
        <root>/run/order          startup order, one service per line
        <root>/run/services       all service names, one per line
"""

import argparse
import json
import os
import re
import sys

# --------------------------------------------------------------------------------------
# minimal YAML subset parser (only used when PyYAML is not importable)
# --------------------------------------------------------------------------------------


class YamlError(Exception):
    pass


def _strip_comment(s):
    out = []
    quote = None
    for ch in s:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            out.append(ch)
        elif ch == '#' and (not out or out[-1] in ' \t'):
            break
        else:
            out.append(ch)
    return ''.join(out).rstrip()


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        return v[1:-1]
    if v in ('true', 'True', 'yes', 'on'):
        return True
    if v in ('false', 'False', 'no', 'off'):
        return False
    if v in ('null', 'Null', '~', ''):
        return None
    if re.fullmatch(r'-?\d+', v):
        return int(v)
    if re.fullmatch(r'-?\d+\.\d+', v):
        return float(v)
    return v


class MiniYaml:
    """Parses the compose subset: nested maps, block lists, scalars, comments."""

    def __init__(self, text):
        self.lines = []
        for raw in text.splitlines():
            if not raw.strip() or raw.lstrip().startswith('#'):
                continue
            body = raw.lstrip(' ')
            if body.startswith('\t'):
                raise YamlError('tabs are not supported for indentation')
            indent = len(raw) - len(body)
            self.lines.append((indent, _strip_comment(body.strip())))
        self.i = 0

    def parse(self):
        if not self.lines:
            return {}
        return self._block(self.lines[0][0])

    def _block(self, indent):
        if self.lines[self.i][1].startswith('- '):
            return self._list(indent)
        return self._map(indent)

    def _map(self, indent):
        out = {}
        while self.i < len(self.lines):
            ind, txt = self.lines[self.i]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError('unexpected indentation: %r' % txt)
            if txt.startswith('- '):
                break
            if ':' not in txt:
                raise YamlError('expected "key: value", got %r' % txt)
            key, _, rest = txt.partition(':')
            key = str(_scalar(key.strip()))
            rest = rest.strip()
            self.i += 1
            if rest == '':
                if self.i < len(self.lines) and self.lines[self.i][0] > ind:
                    out[key] = self._block(self.lines[self.i][0])
                else:
                    out[key] = None
            else:
                out[key] = _scalar(rest)
        return out

    def _list(self, indent):
        out = []
        while self.i < len(self.lines):
            ind, txt = self.lines[self.i]
            if ind < indent:
                break
            if ind > indent:
                raise YamlError('unexpected indentation in list: %r' % txt)
            if not txt.startswith('- '):
                break
            item = txt[2:].strip()
            # `- key: value` starts a map item; `- 2283:2283` / `- ./x:/y` are scalars
            if re.match(r'^[A-Za-z_][A-Za-z0-9_.\-]*:\s*$', item) or re.match(
                r'^[A-Za-z_][A-Za-z0-9_.\-]*:\s+\S', item
            ):
                self.lines[self.i] = (ind + 2, item)
                out.append(self._map(ind + 2))
            else:
                self.i += 1
                out.append(_scalar(item))
        return out


def load_yaml(path):
    with open(path, 'r', encoding='utf-8') as fh:
        text = fh.read()
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}, 'PyYAML'
    except ImportError:
        return MiniYaml(text).parse(), 'builtin'


# --------------------------------------------------------------------------------------
# normalisation helpers
# --------------------------------------------------------------------------------------


def norm_env(env):
    out = {}
    if not env:
        return out
    if isinstance(env, dict):
        for key, val in env.items():
            if val is None:
                out[str(key)] = ''
            elif isinstance(val, bool):
                out[str(key)] = 'true' if val else 'false'
            else:
                out[str(key)] = str(val)
    else:
        for item in env:
            key, _, val = str(item).partition('=')
            out[key] = val
    return out


def norm_ports(ports):
    out = []
    if not ports:
        return out
    if isinstance(ports, (str, int)):
        ports = [ports]
    for entry in ports:
        if isinstance(entry, dict):
            continue
        parts = str(entry).split(':')
        try:
            if len(parts) == 1:
                out.append({'ip': None, 'host': int(parts[0]), 'container': int(parts[0])})
            elif len(parts) == 2:
                out.append({'ip': None, 'host': int(parts[0]), 'container': int(parts[1].split('/')[0])})
            else:
                out.append(
                    {
                        'ip': parts[0],
                        'host': int(parts[1]),
                        'container': int(parts[2].split('/')[0]),
                    }
                )
        except ValueError:
            continue
    return out


def norm_volumes(volumes, bind_base, volume_base):
    out = []
    if not volumes:
        return out
    for entry in volumes:
        read_only = False
        if isinstance(entry, dict):
            src = entry.get('source')
            dst = entry.get('target')
            kind = entry.get('type') or 'bind'
            read_only = bool(entry.get('read_only'))
            if not src or not dst:
                continue
        else:
            parts = str(entry).split(':')
            if len(parts) < 2:
                continue
            src, dst = parts[0], parts[1]
            kind = 'bind' if src.startswith(('.', '/')) else 'volume'
            read_only = len(parts) > 2 and 'ro' in parts[2]
        if src.startswith('/'):
            host = src
            kind = 'bind'
            origin = 'absolute'
        elif src.startswith('.'):
            host = os.path.normpath(os.path.join(bind_base, src))
            kind = 'bind'
            origin = 'relative'
        else:
            host = os.path.normpath(os.path.join(volume_base, src))
            kind = 'volume'
            origin = 'volume'
        out.append(
            {
                'src': host,
                'dst': dst,
                'kind': kind,
                'origin': origin,
                'mode': 'ro' if read_only else 'rw',
            }
        )
    return out


def norm_depends_on(depends):
    if not depends:
        return []
    if isinstance(depends, dict):
        return list(depends.keys())
    if isinstance(depends, str):
        return [depends]
    return [str(x) for x in depends]


def image_repo(image):
    if not image:
        return ''
    ref = image.split('@', 1)[0]
    tail = ref.rsplit('/', 1)[-1]
    if ':' in tail:
        ref = ref.rsplit(':', 1)[0]
    return ref


def role_of(image):
    base = image_repo(image).rsplit('/', 1)[-1].lower()
    if 'immich-server' in base:
        return 'server'
    if 'immich-machine-learning' in base:
        return 'ml'
    if 'valkey' in base or 'redis' in base:
        return 'redis'
    if 'postgres' in base:
        return 'database'
    return 'generic'


def run_as_user(role):
    return {'database': ['postgres'], 'redis': ['redis', 'valkey', 'valkey-server']}.get(role, [])


ROLE_ORDER = {'database': 0, 'redis': 1, 'server': 2, 'ml': 3, 'generic': 2}

# Services without a `ports:` entry still listen inside the container; these are the
# upstream defaults (same ones immich documents for IMMICH_PORT / Postgres / Redis).
DEFAULT_PORTS = {'database': 5432, 'redis': 6379, 'server': 2283, 'ml': 3003}


def startup_order(services):
    """Topological sort honouring depends_on; ML starts after the API server."""

    # immich-machine-learning has no depends_on, but there is no reason to boot it
    # before the API server (and on a phone we want the API answering first).
    has_server = any(svc['role'] == 'server' for svc in services.values())
    servers = [name for name, svc in services.items() if svc['role'] == 'server']
    effective = {}
    for name, svc in services.items():
        deps = list(svc['depends_on'])
        if has_server and svc['role'] == 'ml':
            deps += [s for s in servers if s not in deps]
        effective[name] = dict(svc, depends_on=deps)

    remaining = dict(effective)
    ordered = []
    while remaining:
        ready = [
            name
            for name, svc in remaining.items()
            if all(dep in ordered or dep not in services for dep in svc['depends_on'])
        ]
        if not ready:
            ready = list(remaining)
        ready.sort(key=lambda n: (ROLE_ORDER.get(remaining[n]['role'], 9), n))
        for name in ready:
            ordered.append(name)
            del remaining[name]
    return ordered


# --------------------------------------------------------------------------------------


def build_plan(compose_path, root, arch):
    doc, parser = load_yaml(compose_path)
    services_doc = doc.get('services') or {}
    if not services_doc:
        raise SystemExit('error: no services found in %s' % compose_path)

    compose_dir = os.path.dirname(os.path.abspath(compose_path))
    bind_base = compose_dir
    volume_base = os.path.join(root, 'volumes')

    services = {}
    for name, spec in services_doc.items():
        spec = spec or {}
        image = str(spec.get('image') or '')
        role = role_of(image)
        services[name] = {
            'name': name,
            'role': role,
            'image': image,
            'container_name': str(spec.get('container_name') or name),
            'entrypoint': spec.get('entrypoint'),
            'command': spec.get('command'),
            'env': norm_env(spec.get('environment')),
            'ports': norm_ports(spec.get('ports')),
            'volumes': norm_volumes(spec.get('volumes'), bind_base, volume_base),
            'depends_on': norm_depends_on(spec.get('depends_on')),
            'default_port': DEFAULT_PORTS.get(role),
            'shm_size': spec.get('shm_size'),
            'restart': spec.get('restart'),
            'healthcheck': spec.get('healthcheck'),
            'user': spec.get('user'),
            'run_as_user': run_as_user(role),
        }

    aliases = []
    for name, svc in services.items():
        for alias in (name, svc['container_name']):
            if alias and alias not in aliases:
                aliases.append(alias)

    plan = {
        'project': str(doc.get('name') or os.path.basename(bind_base.rstrip('/'))),
        'compose_file': os.path.abspath(compose_path),
        'compose_dir': bind_base,
        'root': root,
        'arch': arch,
        'yaml_parser': parser,
        'hosts_aliases': aliases,
        'services': services,
        'declared_volumes': sorted((doc.get('volumes') or {}).keys()),
    }
    return plan


def write_runtime_specs(plan):
    root = plan['root']
    run_dir = os.path.join(root, 'run')
    os.makedirs(run_dir, exist_ok=True)

    for name, svc in plan['services'].items():
        spec = dict(svc)
        spec['rootfs'] = os.path.join(root, 'rootfs', name)
        spec['project'] = plan['project']
        spec['arch'] = plan['arch']
        spec['hosts_aliases'] = plan['hosts_aliases']
        with open(os.path.join(run_dir, name + '.json'), 'w', encoding='utf-8') as fh:
            json.dump(spec, fh, indent=2, sort_keys=True)
            fh.write('\n')

    order = startup_order(plan['services'])
    with open(os.path.join(run_dir, 'order'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(order) + '\n')
    with open(os.path.join(run_dir, 'services'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(plan['services']) + '\n')
    return order


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--compose', default='docker-compose.yaml')
    ap.add_argument('--root', required=True)
    ap.add_argument('--arch', default='arm64')
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    plan = build_plan(args.compose, root, args.arch)
    order = write_runtime_specs(plan)

    with open(os.path.join(root, 'plan.json'), 'w', encoding='utf-8') as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
        fh.write('\n')

    print('project      : %s' % plan['project'])
    print('yaml parser  : %s' % plan['yaml_parser'])
    for name in order:
        svc = plan['services'][name]
        print(
            '  %-24s role=%-9s image=%-52s ports=%s'
            % (name, svc['role'], svc['image'][:52], svc['ports'] or '-')
        )
    print('plan written : %s/plan.json' % root)
    return 0


if __name__ == '__main__':
    sys.exit(main())