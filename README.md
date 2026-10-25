# immich-termux — Immich on Android without Docker

Run the **official Immich arm64 OCI images** on Android/Termux. No Docker, no Podman,
no root, no `proot-distro` distro tarball.

```
./install.sh          # bootstrap Termux, translate the compose file, pull images
./immich up           # start the stack
./immich status
```

Then open `http://<phone-ip>:2283`.

Verified end-to-end on a non-rooted Android 10 device (aarch64, kernel 4.14, 3.7 GB RAM):
Postgres 14 + VectorChord, Valkey, and Immich **v3.3.5** API/microservices all running,
`GET /api/server/ping` → `pong`, web UI served on `:2283`.

---

## Why not Docker?

Android removed the features containers are built on. Measured on the test device:

```
$ unshare -m true
unshare: unshare failed: Operation not permitted
```

No unprivileged namespaces ⇒ no `runc`/`containerd`/Podman, whatever binary you feed it.
And no amount of static linking turns a 4-service compose stack (Node.js + Python/ONNX +
C + Postgres) into a single executable.

What Android *does* allow is `ptrace`, which is all **proot** needs. So instead of
containerising, this project:

1. parses `docker-compose.yaml`,
2. pulls each image from its registry over plain HTTPS (`lib/oci_pull.py`; no docker,
   no skopeo — anonymous token flow + digest verification),
3. extracts each image into its own rootfs directory,
4. runs each service's **real entrypoint** under `proot`.

proot does not isolate the network, so every service shares Termux's network namespace —
the same flat topology a compose bridge network gives you. Container-name DNS (`database`,
`redis`, `immich-machine-learning`) is emulated by writing the aliases into each rootfs's
`/etc/hosts`, so the upstream images run with their stock defaults.

```
docker-compose.yml          this project
──────────────────          ────────────────────────────────────────────────
service      ────────────►  proot -r rootfs/<service>  <image entrypoint>
image pull   ────────────►  registry HTTPS → cache/blobs → tar → rootfs/<service>
named volume ────────────►  ./volumes/<name>   (bind)
./path bind  ────────────►  ./path             (bind, created if missing)
bridge net   ────────────►  Termux loopback (all services on 127.0.0.1)
container DNS────────────►  /etc/hosts aliases in each rootfs
healthcheck  ────────────►  TCP / RESP PING / HTTP ping probes (lib/probe.py)
depends_on   ────────────►  startup order + wait-for-healthy loop
```

## The five Android-specific blockers (and how they are handled)

Everything below was found by actually running the stack, not from documentation.

| # | Blocker | Symptom | Fix in this repo |
|---|---|---|---|
| 1 | No namespaces | `unshare: Operation not permitted` | `proot` (ptrace based) instead of a container runtime |
| 2 | **Hard links are denied to apps** (`link(2)` → `EACCES`) | `tar: Cannot hard link to 'usr/bin/perl': Permission denied` while extracting images | `lib/oci_pull.py` extracts with Python's `tarfile` and materialises hard links as file copies |
| 3 | `termux-exec`'s `LD_PRELOAD` shim breaks proot's tracees | `proot error: execve(...): Function not implemented` | the launcher always runs proot with `LD_PRELOAD` cleared |
| 4 | **SysV IPC is blocked by Android's seccomp policy** (`shmget` → `SIGSYS`/`ENOSYS`) | `FATAL: could not create shared memory segment: Function not implemented` / `DETAIL: Failed system call was shmget(key=..., size=56, 03600)` | `shm/shm_compat.c` — a freestanding `LD_PRELOAD` shim (same approach as Termux's `libandroid-shmem`) that implements `shmget/shmat/shmdt/shmctl` over `MAP_SHARED` files; installed via the rootfs's `/etc/ld.so.preload` |
| 5 | `pgvecto.rs` (`vectors.so`) panics on Android | `ERROR: called Result::unwrap() on an Err value: Os { code: 13, kind: PermissionDenied }` in `vectors::ipc::transport::mmap::connect`, then `PANIC: ERRORDATA_STACK_SIZE exceeded` → backend aborted | only `vchord.so` is preloaded (`PG_PRELOAD`), which is what Immich v3 uses anyway |

Worth knowing about #4: PostgreSQL needs SysV shared memory **even with
`shared_memory_type = mmap`**, because it reserves a minimal block
(`sizeof(PGShmemHeader)` = 56 bytes) there for cross-process discovery. There is no
configuration switch that avoids it — in PostgreSQL 14 through 18 the code path is
unconditional. Hence the shim.

Two smaller ones: Android has no `/dev/shm` (a directory is bind-mounted into it), and no
usable `/usr/share/zoneinfo` (the `tzdata` wheel is copied into every rootfs so containers
stop logging in UTC).

## Requirements

| | |
|---|---|
| Device | aarch64 Android, Termux from F-Droid/GitHub (not the Play Store build) |
| RAM | 4 GB is the practical floor with machine learning **disabled** (upstream minimum is 6 GB) |
| Storage | ~3 GB for images/rootfs plus your photo library |
| Termux packages | `proot`, `python`, `curl`, `tar` (installed by `install.sh`); `clang` only if you want to build the shim yourself |

## Install

```bash
cd ~ && git clone <this repo> immich-termux && cd immich-termux
./install.sh              # server + database + redis
./install.sh --with-ml    # also the machine-learning container (see "Machine learning")
```

`install.sh` will:

1. `pkg install -y proot python` (curl/tar ship with Termux),
2. run `immich plan` — translates `docker-compose.yaml` into `run/*.json`,
3. pull and extract the images (~600 MB without machine learning),
4. build the SysV shim if `clang` is available, otherwise use the bundled aarch64 build,
5. install the `tzdata` wheel and copy real TZif files into each rootfs.

Then keep it alive — Android freezes background apps:

```bash
pkg install termux-api      # then, once per boot:
termux-wake-lock
```

and disable battery optimisation for Termux in Android settings.

## Commands

```
immich install [--with-ml]     bootstrap everything
immich plan                    docker-compose.yaml -> run/*.json (+ plan.json)
immich pull [svc...] [--force] pull/extract images (no docker)
immich up [svc...]             start + wait for health
immich down                    stop, reverse dependency order
immich restart [svc...]
immich supervise             keep the stack alive: restart dead services (Ctrl-C stops all)
immich status                  state, pid, uptime, port, health  (alias: ps)
immich logs <svc> [lines|-f]   log tail
immich shell <svc>             shell inside the service rootfs
immich exec <svc> -- cmd ...   run a command inside the rootfs
immich env <svc>               resolved container environment
immich url [svc]               LAN URL
immich doctor                  device/kernel/plan diagnostics
immich prune                   delete the downloaded layer cache
```

Examples:

```bash
./immich logs immich-server -f
./immich shell database -- psql -U postgres -d immich -c 'select version()'
./immich exec immich-server -- immich-admin list-users
```

The plan is regenerated automatically when `docker-compose.yaml` changes.

## Configuration

Copy `immich.conf.example` to `immich.conf` (or export the variables).

| Key | Default | Meaning |
|---|---|---|
| `WITH_ML` | `0` | run `immich-machine-learning` (smart search, face recognition) |
| `ARCH` | `arm64` | OCI platform to pull |
| `DNS_SERVERS` | `1.1.1.1 8.8.8.8 9.9.9.9` | written to each rootfs's `/etc/resolv.conf` — Android's netd resolver is invisible to glibc inside proot |
| `NODE_HEAP_MB` | `768` | `--max-old-space-size` for the API/microservices workers |
| `IMMICH_BIND_HOST` | `0.0.0.0` | bind address (`0.0.0.0` = reachable from your LAN) |
| `PG_LOW_MEM` | `1` | regenerate `/etc/postgresql/postgresql.conf` with 192 MB `shared_buffers` (template says 512 MB) |
| `PG_PRELOAD` | `vchord.so` | extensions PostgreSQL preloads (pgvecto.rs crashes on Android) |
| `SHM_COMPAT` | `1` | install/activate the SysV shared memory shim (required) |
| `HEALTH_TIMEOUT` | `180` | seconds to wait for a service to become healthy |
| `TZ` | from compose | timezone written to `/etc/localtime` |
| `PG_LOG_STATEMENT`, `RUST_BACKTRACE` | – | debugging only |

## What the phone actually runs

| Service | Image | Notes |
|---|---|---|
| `immich-server` | `ghcr.io/immich-app/immich-server:v3` | `tini` → `start.sh` → two Node workers (`api`, `microservices`), web UI + API on `:2283` |
| `database` | `ghcr.io/immich-app/postgres:14-vectorchord…` | PostgreSQL 14 with VectorChord; `shared_memory_type=mmap` + the SysV shim |
| `redis` | `valkey/valkey:9` | pinned by digest in the compose file |
| `immich-machine-learning` | `ghcr.io/immich-app/immich-machine-learning:v3` | optional, `python -m immich_ml` on `:3003` |

Service roles are detected from the image name, so an Immich compose file that
adds/renames services still works; unknown services get the generic treatment (image
entrypoint, binds, rootfs, health probe by TCP).

Measured on the test device: cold `immich up` of all three services ≈ **113 s** (Postgres
initdb ~40 s the very first time, Immich migrations + geodata import ~3–5 min on the very
first boot). Steady state: ~830 MB resident + heavy zram swap use; the API answers in
well under a second. Stability checks run against it: 40/40 SQL queries, 18/18 API pings
over 90 s, a VectorChord index query, and `immich supervise` restarting a service that was
killed underneath it.

## Differences from `docker compose`

- **Isolation is emulated, not real.** One namespace, one UID mapping. Any port in the
  compose file is really bound on the device; `8080:2283` is honoured by setting
  `IMMICH_PORT=8080`, but published ports for databases/caches cannot be remapped.
- **`restart: always` is not a supervisor.** Nothing restarts after a reboot: run
  `./immich up`, or keep a supervisor in the foreground with `./immich supervise`
  (it restarts dead services; `Ctrl-C` stops the whole stack), and/or wire `up` into
  `~/.termux/boot/`.
- **`/dev/shm` is a directory, not tmpfs.** A container always gets a fresh tmpfs;
  here it is `volumes/shm` and it survives restarts, so a cold `immich up` empties it
  (PostgreSQL keeps its dynamic shared memory control segment there and reusing a stale
  one can crash backends).
- **No image layer dedup.** Each service gets its own extracted rootfs, so base layers
  are duplicated (~2.5 GB total). `immich prune` reclaims the download cache.
- **`healthcheck:` entries are not executed**; equivalent probes are used
  (`redis-cli ping` → RESP `PING`, immich healthcheck → `GET /api/server/ping`).
- **`/etc/localtime`** from the compose file is ignored (Android has no zone file); the
  timezone comes from `TZ` + the `tzdata` wheel instead.
- **`/etc/postgresql/postgresql.conf`** is regenerated on every start (Android needs
  `shared_memory_type = mmap`); edit `immich.conf` instead of that file.

## Machine learning

Upstream Immich wants 6 GB RAM minimum (8 GB recommended) for the ML container; the test
device has 3.7 GB, so `WITH_ML=0` is the default and smart search / facial recognition are
off. Everything else works: upload, browse, albums, sharing, transcoding, metadata,
mobile app.

If you have a 6 GB+ phone:

```bash
./immich pull immich-machine-learning
WITH_ML=1 ./immich up
```

The models are downloaded into `volumes/model-cache` on first use.

## Troubleshooting

**`proot error: execve(...): Function not implemented`** — `termux-exec`'s `LD_PRELOAD`
shim breaks proot's tracees. The launcher clears `LD_PRELOAD` before invoking proot; if
you run proot by hand, do the same (`env -u LD_PRELOAD proot …`).

**`tar: Cannot hard link to …: Permission denied`** — Android denies `link(2)` to apps.
The extractor materialises hard links as copies; that is why it does not shell out to `tar`.

**`FATAL: could not create shared memory segment … shmget(...)`** — the SysV shim is not
active. Check `immich doctor` (`sysv shim`), then:

```bash
./immich exec database -- ls -l /etc/ld.so.preload /usr/local/lib/libshm-compat.so
```

If it is missing, build it: `cd shm && clang -shared -fPIC -nostdlib -nodefaultlibs
-nostartfiles -O2 -o libshm-compat.so shm_compat.c` (or keep the bundled
`lib/prebuilt/arm64/libshm-compat.so`).

**`Result::unwrap() ... PermissionDenied` + `PANIC: ERRORDATA_STACK_SIZE exceeded`** —
`vectors.so` (pgvecto.rs) is preloaded. Immich v3 uses VectorChord; keep
`PG_PRELOAD=vchord.so`.

**`immich up` hangs at "did not become healthy"** — look at the log tail it prints and at
`logs/<service>.log`. Usual causes: full disk, missing `proot`, DNS inside a container
(fix `DNS_SERVERS`), or the device swapping so hard that startup exceeds
`HEALTH_TIMEOUT`.

**The database disappears after a while** — check `logs/database.log` for
`terminated with signal`. If you restarted the stack while a previous Postgres was still
half-alive, stale state can linger; `./immich down` then `./immich up` gives you a clean
start (cold start also clears `/dev/shm`). Keep `./immich supervise` running for
unattended use.

**Everything is UTC** — `pip install tzdata`, then `./immich install` again (or
`python3 lib/tzsetup.py --root . --tz Asia/Jakarta`).

**The stack stops when the screen sleeps** — `termux-wake-lock` and exempt Termux from
battery optimisation.

## Repository layout

```
immich                  bash CLI (deps, orchestration, health, status)
docker-compose.yaml     the input: the upstream Immich compose file
install.sh              bootstrap wrapper
immich.conf.example     configuration template
lib/compose2plan.py     compose YAML -> run/<service>.json (+ plan.json)
lib/oci_pull.py         registry client + hardlink-safe layer extractor
lib/svc.py              builds and supervises the proot command line
lib/probe.py            TCP / RESP / HTTP health probes
lib/tzsetup.py          real /etc/localtime + /usr/share/zoneinfo in every rootfs
lib/prebuilt/arm64/     prebuilt SysV shared memory shim
shm/shm_compat.c        the shim source (shmget/shmat/shmdt/shmctl over mmap)
shm/shm_test.c          self-test for the shim
run/                    generated: per-service spec/env, pid files, resolv.conf
rootfs/<service>/       extracted image filesystem (one per service)
volumes/<name>/         compose named volumes (e.g. model-cache)
cache/blobs/            downloaded, digest-verified layers
logs/<service>.log      service stdout/stderr
```

## Licence

The scripts in this repository are provided as-is. The container images pulled by it keep
their own licences (Immich is AGPL-3.0, PostgreSQL, Valkey BSD).
