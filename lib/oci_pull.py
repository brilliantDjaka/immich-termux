#!/bin/sh
# Termux has no /usr/bin/env: the line below is a shell/python polyglot that re-execs
# this file under python3 (in Python it is just a string literal).
''''exec python3 "$0" "$@" #'''
"""
oci_pull.py -- dockerless OCI image puller + rootfs extractor for Termux.

Downloads an image straight from the registry over HTTPS (anonymous token flow),
picks the manifest for the requested architecture, streams every layer through
`tar` into <root>/rootfs/<service>, and records the image config for lib/svc.py.

No docker, no skopeo, no containerd: just curl + tar + this file.

  oci_pull.py --root DIR --spec run/immich-server.json [--force] [--dry-run]
  oci_pull.py --root DIR --all
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile

DOCKER_HUB = 'registry-1.docker.io'
TOKEN_SERVICE = {
    'ghcr.io': 'https://ghcr.io/token?scope=repository:{repo}:pull&service=ghcr.io',
    'registry-1.docker.io': 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull',
}
MANIFEST_ACCEPT = ','.join(
    [
        'application/vnd.oci.image.index.v1+json',
        'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json',
        'application/vnd.docker.distribution.manifest.v2+json',
    ]
)


def log(msg):
    print(msg, flush=True)


def curl_json(url, token, accept=None):
    argv = ['curl', '-sSL', '--retry', '3', '--retry-delay', '2', '--connect-timeout', '20']
    if accept:
        argv += ['-H', 'Accept: ' + accept]
    if token:
        argv += ['-H', 'Authorization: Bearer ' + token]
    argv.append(url)
    out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if out.returncode != 0:
        raise SystemExit('error: curl failed for %s\n%s' % (url, out.stderr.decode()))
    try:
        return json.loads(out.stdout.decode())
    except ValueError:
        raise SystemExit('error: non-JSON response from %s:\n%s' % (url, out.stdout[:400].decode(errors='replace')))


def curl_download(url, token, dest):
    tmp = dest + '.part'
    argv = [
        'curl', '-fSL', '--retry', '3', '--retry-delay', '2', '--connect-timeout', '20',
        '--progress-bar', '-H', 'Authorization: Bearer ' + token, '-o', tmp, url,
    ]
    # progress bar goes to stderr; keep it visible
    rc = subprocess.run(argv).returncode
    if rc != 0:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise SystemExit('error: download failed for %s' % url)
    os.replace(tmp, dest)


def parse_image(ref):
    """ghcr.io/immich-app/immich-server:v3  ->  (ghcr.io, immich-app/immich-server, v3)"""
    digest = None
    if '@' in ref:
        ref, digest = ref.split('@', 1)
    tag = None
    head, _, tail = ref.rpartition('/')
    if ':' in tail:
        tag = tail.rsplit(':', 1)[1]
        ref = (head + '/' + tail.rsplit(':', 1)[0]) if head else tail.rsplit(':', 1)[0]
    elif '.' in tail or ':' in tail:
        # registry:port/repo form without tag
        pass
    parts = ref.split('/', 1)
    if len(parts) == 2 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        registry, repo = parts[0], parts[1]
    else:
        registry, repo = 'docker.io', ref
    if registry in ('docker.io', 'index.docker.io'):
        registry = DOCKER_HUB
        if '/' not in repo:
            repo = 'library/' + repo
    reference = digest or tag or 'latest'
    return registry, repo, reference, tag


def token_for(registry, repo):
    svc = TOKEN_SERVICE.get(registry)
    url = (svc or 'https://{host}/token?scope=repository:{repo}:pull&service={host}').format(
        host=registry, repo=repo
    )
    data = curl_json(url, None)
    tok = data.get('token') or data.get('access_token')
    if not tok:
        raise SystemExit('error: could not obtain registry token for %s' % repo)
    return tok


def resolve_manifest(registry, repo, reference, token, arch):
    base = 'https://%s/v2/%s' % (registry, repo)
    doc = curl_json('%s/manifests/%s' % (base, reference), token, MANIFEST_ACCEPT)
    if 'manifests' in doc:
        want = [(arch, 'linux'), ('arm64', 'linux')] if arch == 'arm64' else [(arch, 'linux')]
        chosen = None
        for a, o in want:
            for entry in doc['manifests']:
                plat = entry.get('platform') or {}
                if plat.get('architecture') == a and plat.get('os') == o:
                    if 'attestation' in json.dumps(entry.get('annotations') or {}):
                        continue
                    chosen = entry
                    break
            if chosen:
                break
        if not chosen:
            raise SystemExit('error: no %s/%s manifest for %s' % (arch, 'linux', repo))
        digest = chosen['digest']
        doc = curl_json('%s/manifests/%s' % (base, digest), token, MANIFEST_ACCEPT)
    return base, doc


def digest_of_file(path, algo='sha256'):
    h = hashlib.new(algo)
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


# Python >= 3.12 asks for an extraction filter. Docker layers legitimately contain
# absolute symlinks (/usr/bin/perl -> /etc/alternatives/perl), which the default 'data'
# filter rejects, and every layer is digest-verified, so trust the archive explicitly.
EXTRACT_KWARGS = {'filter': 'fully_trusted'} if sys.version_info >= (3, 12) else {}


def safe_name(name):
    """Reject absolute paths and traversals without mangling legitimate dot-files."""
    if not name:
        return None
    if name.startswith('/'):
        return None
    while name.startswith('./'):
        name = name[2:]
    if not name or name in ('.', '..'):
        return None
    if '..' in name.split('/'):
        return None
    return name


def extract_layer(blob, rootfs, quiet=False):
    """Extract a layer into the rootfs.

    Android does not let apps create hard links (EACCES on link(2)), and OCI layers do
    contain them (perl, coreutils multi-call binaries, ...), so hard links are
    materialised as file copies.  Device nodes are skipped: /dev is bound at run time.
    """
    os.makedirs(rootfs, exist_ok=True)
    rootfs = os.path.abspath(rootfs)
    skips = 0
    with tarfile.open(blob, 'r:*') as tf:
        for member in tf:
            name = safe_name(member.name)
            if name is None or name.split('/')[0] in ('dev', 'proc', 'sys'):
                continue
            if member.isdev() or member.ischr() or member.isblk():
                continue
            target = os.path.join(rootfs, name)
            if member.islnk():
                copy_hardlink(tf, member, rootfs, target, quiet=quiet)
                continue
            try:
                tf.extract(member, rootfs, set_attrs=True, numeric_owner=False, **EXTRACT_KWARGS)
            except (PermissionError, FileExistsError) as exc:
                skips += 1
                if not quiet:
                    print('  warn: %s: %s' % (name, exc), file=sys.stderr)
            except OSError as exc:
                raise SystemExit('error: extracting %s from %s failed: %s' % (name, blob, exc))
    if skips and not quiet:
        print('  note: %d entry/entries skipped during extraction' % skips, file=sys.stderr)
    apply_whiteouts(rootfs)


def copy_hardlink(tf, member, rootfs, target, quiet=False):
    """Materialise a tar hard link entry as a copy of its target file."""
    linkname = safe_name(member.linkname)
    source = os.path.join(rootfs, linkname) if linkname else None
    if not source or not os.path.exists(source):
        try:
            resolved = tf._find_link_target(member)  # noqa: SLF001 (tarfile internal, stable)
            source = os.path.join(rootfs, safe_name(resolved.name) or '')
        except Exception:
            return
    if not os.path.exists(source):
        return
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            os.unlink(target)
        shutil.copyfile(source, target)
        shutil.copymode(source, target)
    except OSError as exc:
        if not quiet:
            print('  warn: hard link %s -> %s failed: %s' % (member.name, source, exc), file=sys.stderr)


def apply_whiteouts(rootfs):
    """Layer deletions are encoded as .wh.<name> entries."""
    for dirpath, dirnames, filenames in os.walk(rootfs, topdown=False):
        for name in filenames:
            if not name.startswith('.wh.'):
                continue
            marker = os.path.join(dirpath, name)
            if name == '.wh..wh..opq':
                # opaque directory: hide lower-layer content. We extract layers into a
                # single tree, so we cannot resolve it exactly; drop the marker and warn.
                os.unlink(marker)
                continue
            target = os.path.join(dirpath, name[4:])
            if os.path.islink(target) or os.path.isfile(target):
                os.unlink(target)
            elif os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            os.unlink(marker)
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if not os.path.isdir(full):
                dirnames.remove(name)


def write_image_meta(rootfs, meta):
    with open(os.path.join(rootfs, '.immich-image.json'), 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
        fh.write('\n')


def pull_image(image, rootfs, cache_dir, arch, force=False, dry_run=False, quiet=False):
    registry, repo, reference, tag = parse_image(image)
    token = token_for(registry, repo)
    base, manifest = resolve_manifest(registry, repo, reference, token, arch)
    config_digest = manifest['config']['digest']
    layers = [layer for layer in manifest['layers'] if layer.get('mediaType', '').endswith(('tar+gzip', 'tar'))]

    cfg = curl_json('%s/blobs/%s' % (base, config_digest), token)
    meta = {
        'image': image,
        'registry': registry,
        'repository': repo,
        'reference': reference,
        'tag': tag,
        'architecture': arch,
        'config_digest': config_digest,
        'config': cfg.get('config') or {},
        'layers': [layer['digest'] for layer in layers],
    }

    total = sum(int(layer.get('size') or 0) for layer in layers)
    log('  image      : %s' % image)
    log('  registry   : %s/%s@%s' % (registry, repo, reference))
    log('  layers     : %d (%.1f MiB compressed)' % (len(layers), total / 1048576.0))
    log('  entrypoint : %s' % ((cfg.get('config') or {}).get('Entrypoint') or []))
    log('  cmd        : %s' % ((cfg.get('config') or {}).get('Cmd') or []))
    if dry_run:
        return meta

    os.makedirs(cache_dir, exist_ok=True)
    for index, layer in enumerate(layers, 1):
        digest = layer['digest']
        algo, _, hexdigest = digest.partition(':')
        blob = os.path.join(cache_dir, '%s.%s.tar' % (algo, hexdigest))
        size = int(layer.get('size') or 0)
        need = force or not os.path.exists(blob) or (size and os.path.getsize(blob) != size)
        if need:
            log('  [%d/%d] downloading %s (%.1f MiB)' % (index, len(layers), digest[:19] + '...', size / 1048576.0))
            curl_download('%s/blobs/%s' % (base, digest), token, blob)
            actual = digest_of_file(blob, algo)
            if actual != hexdigest:
                os.unlink(blob)
                raise SystemExit('error: checksum mismatch for %s (got %s)' % (digest, actual))
        elif not quiet:
            log('  [%d/%d] cached    %s' % (index, len(layers), digest[:19] + '...'))
        extract_layer(blob, rootfs, quiet=quiet)

    write_image_meta(rootfs, meta)
    # keep the effective rootfs entrypoint/cmd where svc.py expects it
    ensure_rootfs_skeleton(rootfs)
    return meta


def ensure_rootfs_skeleton(rootfs):
    for path, mode in (('dev', 0o755), ('proc', 0o755), ('sys', 0o755), ('tmp', 0o1777)):
        full = os.path.join(rootfs, path)
        if not os.path.exists(full):
            os.makedirs(full, exist_ok=True)
    for path in ('etc/hosts', 'etc/resolv.conf'):
        full = os.path.join(rootfs, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if not os.path.exists(full):
            open(full, 'a').close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', required=True)
    ap.add_argument('--spec', action='append', default=[], help='run/<service>.json to pull')
    ap.add_argument('--all', action='store_true', help='pull every service from run/*.json')
    ap.add_argument('--arch', default='arm64')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    specs = list(args.spec)
    if args.all or not specs:
        run_dir = os.path.join(root, 'run')
        specs = sorted(
            os.path.join('run', f) for f in os.listdir(run_dir) if f.endswith('.json')
        )
        if not specs:
            raise SystemExit('error: no specs in %s -- run `immich plan` first' % run_dir)

    cache_dir = os.path.join(root, 'cache', 'blobs')
    seen = {}
    for spec_path in specs:
        full = spec_path if os.path.isabs(spec_path) else os.path.join(root, spec_path)
        with open(full, 'r', encoding='utf-8') as fh:
            spec = json.load(fh)
        if not spec.get('image'):
            log('skip %s: no image' % spec.get('name'))
            continue
        rootfs = spec.get('rootfs') or os.path.join(root, 'rootfs', spec['name'])
        if spec['image'] in seen:
            log('= %s (same image as %s)' % (spec['name'], seen[spec['image']]))
            if not os.path.exists(os.path.join(rootfs, '.immich-image.json')):
                shutil.copytree(os.path.join(root, 'rootfs', seen[spec['image']]), rootfs, dirs_exist_ok=True)
            continue
        log('> %s' % spec['name'])
        pull_image(spec['image'], rootfs, cache_dir, args.arch, force=args.force, dry_run=args.dry_run, quiet=args.quiet)
        seen[spec['image']] = spec['name']
    return 0


if __name__ == '__main__':
    sys.exit(main())