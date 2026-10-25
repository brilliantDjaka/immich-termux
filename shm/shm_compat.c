/*
 * shm_compat.c -- SysV shared memory for Android/Termux processes.
 *
 * Android's app seccomp policy kills shmget(2)/shmat(2)/shmdt(2)/shmctl(2) with
 * SIGSYS, so every program that uses System V shared memory (PostgreSQL, PostgreSQL's
 * initdb, and anything that links libpq-based servers) dies with:
 *
 *     FATAL: could not create shared memory segment: Function not implemented
 *     DETAIL: Failed system call was shmget(key=..., size=56, 03600).
 *
 * PostgreSQL needs SysV shared memory even when shared_memory_type=mmap, because it
 * reserves a minimal block (sizeof(PGShmemHeader) == 56 bytes) there for cross-process
 * discovery of the anonymous segment.
 *
 * This file re-implements the four missing calls on top of MAP_SHARED file mappings in
 * a directory (default /dev/shm/sysv, override with SHM_COMPAT_DIR), which is what
 * libandroid-shmem does for bionic binaries. It is LD_PRELOADed into a glibc guest
 * inside proot, so it is written without any libc headers and without linking libc:
 * the few libc functions it calls (open/mmap/lseek/...) are resolved from the guest's
 * own libc at load time.
 *
 * Build (Termux clang, on device):
 *     clang -shared -fPIC -nostdlib -nodefaultlibs -nostartfiles -O2 \
 *           -Wl,-soname,libshm-compat.so -o libshm-compat.so shm_compat.c
 *
 * Semantics implemented: IPC_CREAT, IPC_EXCL, IPC_STAT, IPC_RMID, attach/detach.
 * Not implemented: permissions beyond 0600/0777 masking, shm_perm uid/gid changes,
 * IPC_SET, SHM_LOCK/SHM_UNLOCK, huge pages.
 */

/* ---- system constants (no headers: the guest's glibc headers are unavailable) ---- */
#define IPC_CREAT 01000
#define IPC_EXCL 02000
#define IPC_RMID 0
#define IPC_SET 1
#define IPC_STAT 2

#define O_RDONLY 00
#define O_RDWR 02
#define O_CREAT 0100
#define O_EXCL 0200
#define SEEK_END 2

#define PROT_READ 0x1
#define PROT_WRITE 0x2
#define MAP_SHARED 0x01
#define MAP_FAILED ((void *)-1)

#define ENOENT 2
#define EACCES 13
#define EFAULT 14
#define EEXIST 17
#define EINVAL 22
#define ENOSPC 28
#define ENOSYS 38

#define MAX_SEGS 64
#define MAX_MAPS 64
#define SHM_DIR_DEFAULT "/dev/shm/sysv"

/* ---- guest libc, resolved at load time ---- */
extern int open(const char *, int, ...);
extern int close(int);
extern void *mmap(void *, unsigned long, int, int, int, long);
extern int munmap(void *, unsigned long);
extern long lseek(int, long, int);
extern int ftruncate(int, long);
extern int unlink(const char *);
extern int mkdir(const char *, unsigned int);
extern int getpid(void);
extern char *getenv(const char *);

/* glibc exports __errno_location; bionic exports __errno. Weak so that this object loads
 * in both (the .so is freestanding and resolves these from the host libc). */
extern int *__errno_location(void) __attribute__((weak));
extern int *__errno(void) __attribute__((weak));

static int *errno_ptr(void)
{
    if (__errno_location)
        return __errno_location();
    return __errno();
}

#define ERRNO (*errno_ptr())

/* ---- structs the caller (glibc layout, aarch64) expects ---- */
struct compat_ipc_perm {
    int key;
    unsigned int uid;
    unsigned int gid;
    unsigned int cuid;
    unsigned int cgid;
    unsigned int mode;
    unsigned short seq;
    unsigned short pad2;
    unsigned long reserved1;
    unsigned long reserved2;
};

struct compat_shmid_ds {
    struct compat_ipc_perm perm;
    unsigned long segsz;
    long atime;
    long dtime;
    long ctime;
    int cpid;
    int lpid;
    unsigned long nattch;
    unsigned long reserved4;
    unsigned long reserved5;
};

struct seg {
    int used;
    int fd;
    int key;
    unsigned int mode;
    int cpid;
    int removed;
    unsigned long size;
    char path[96];
};

struct map {
    int used;
    void *addr;
    unsigned long size;
};

static struct seg g_segs[MAX_SEGS];
static struct map g_maps[MAX_MAPS];
static char g_dir[96];
static int g_dir_ready;

/* ---- tiny helpers ---- */
static unsigned long s_len(const char *s)
{
    unsigned long n = 0;
    while (s && s[n])
        n++;
    return n;
}

static void s_copy(char *dst, const char *src, unsigned long max)
{
    unsigned long i = 0;
    if (!src)
        src = "";
    while (src[i] && i + 1 < max) {
        dst[i] = src[i];
        i++;
    }
    dst[i] = 0;
}

static void hex32(unsigned int value, char *out)
{
    static const char digits[] = "0123456789abcdef";
    int i;
    for (i = 7; i >= 0; i--) {
        out[i] = digits[value & 0xf];
        value >>= 4;
    }
    out[8] = 0;
}

static const char *shm_dir(void)
{
    if (!g_dir_ready) {
        const char *env = getenv("SHM_COMPAT_DIR");
        if (env && env[0])
            s_copy(g_dir, env, sizeof(g_dir));
        else
            s_copy(g_dir, SHM_DIR_DEFAULT, sizeof(g_dir));
        mkdir(g_dir, 0700);
        g_dir_ready = 1;
    }
    return g_dir;
}

static void seg_path(char *out, unsigned long max, int key)
{
    const char *dir = shm_dir();
    char hex[9];
    hex32((unsigned int)key, hex);
    s_copy(out, dir, max);
    unsigned long n = s_len(out);
    if (n + 10 < max) {
        out[n++] = '/';
        for (int i = 0; i < 8; i++)
            out[n++] = hex[i];
        out[n] = 0;
    }
}

static struct seg *find_seg(int key)
{
    for (int i = 0; i < MAX_SEGS; i++) {
        if (g_segs[i].used && !g_segs[i].removed && g_segs[i].key == key)
            return &g_segs[i];
    }
    return 0;
}

static int register_seg(int fd, int key, unsigned int mode, const char *path)
{
    for (int i = 0; i < MAX_SEGS; i++) {
        if (!g_segs[i].used) {
            g_segs[i].used = 1;
            g_segs[i].fd = fd;
            g_segs[i].key = key;
            g_segs[i].mode = mode;
            g_segs[i].cpid = getpid();
            g_segs[i].removed = 0;
            g_segs[i].size = 0;
            s_copy(g_segs[i].path, path, sizeof(g_segs[i].path));
            return i + 1;
        }
    }
    return -1;
}

/* ---- the four emulated calls ---- */

int shmget(int key, unsigned long size, int shmflg)
{
    char path[96];
    int fd;
    int create = (shmflg & IPC_CREAT) != 0;
    int excl = (shmflg & IPC_EXCL) != 0;
    unsigned int mode = shmflg & 0777;

    if (mode == 0)
        mode = 0600;

    seg_path(path, sizeof(path), key);

    struct seg *existing = find_seg(key);
    if (existing && !excl)
        return (int)(existing - g_segs) + 1;

    if (create) {
        int flags = O_RDWR | O_CREAT | (excl ? O_EXCL : 0);
        fd = open(path, flags, mode);
    } else {
        fd = open(path, O_RDWR);
    }

    if (fd < 0)
        return -1; /* EEXIST / ENOENT / EACCES from open() */

    if (create) {
        unsigned long want = size;
        if (want == 0)
            want = 1;
        if (ftruncate(fd, (long)want) != 0) {
            int saved = ERRNO;
            close(fd);
            ERRNO = saved;
            return -1;
        }
    }

    int shmid = register_seg(fd, key, mode, path);
    if (shmid < 0) {
        close(fd);
        ERRNO = ENOSPC;
        return -1;
    }
    return shmid;
}

void *shmat(int shmid, const void *shmaddr, int shmflg)
{
    if (shmid < 1 || shmid > MAX_SEGS || !g_segs[shmid - 1].used || g_segs[shmid - 1].removed) {
        ERRNO = EINVAL;
        return MAP_FAILED;
    }
    struct seg *seg = &g_segs[shmid - 1];
    long size = lseek(seg->fd, 0, SEEK_END);
    if (size <= 0)
        size = (long)seg->size;
    if (size <= 0)
        size = 1;
    seg->size = (unsigned long)size;

    void *addr = mmap((void *)shmaddr, (unsigned long)size, PROT_READ | PROT_WRITE,
                      MAP_SHARED, seg->fd, 0);
    if (addr == MAP_FAILED)
        return MAP_FAILED;

    for (int i = 0; i < MAX_MAPS; i++) {
        if (!g_maps[i].used) {
            g_maps[i].used = 1;
            g_maps[i].addr = addr;
            g_maps[i].size = (unsigned long)size;
            break;
        }
    }
    return addr;
}

int shmdt(const void *shmaddr)
{
    for (int i = 0; i < MAX_MAPS; i++) {
        if (g_maps[i].used && g_maps[i].addr == shmaddr) {
            int rc = munmap(g_maps[i].addr, g_maps[i].size);
            if (rc != 0)
                return rc;
            g_maps[i].used = 0;
            return 0;
        }
    }
    ERRNO = EINVAL;
    return -1;
}

int shmctl(int shmid, int cmd, void *buf)
{
    if (shmid < 1 || shmid > MAX_SEGS || !g_segs[shmid - 1].used) {
        ERRNO = EINVAL;
        return -1;
    }
    struct seg *seg = &g_segs[shmid - 1];

    if (seg->removed) {
        ERRNO = EINVAL;
        return -1;
    }

    if (cmd == IPC_STAT) {
        if (!buf) {
            ERRNO = EFAULT;
            return -1;
        }
        struct compat_shmid_ds *ds = (struct compat_shmid_ds *)buf;
        unsigned char *raw = (unsigned char *)buf;
        for (unsigned long i = 0; i < sizeof(struct compat_shmid_ds); i++)
            raw[i] = 0;
        ds->perm.key = seg->key;
        ds->perm.mode = seg->mode;
        ds->segsz = seg->size ? seg->size : 1;
        ds->cpid = seg->cpid;
        ds->nattch = 1;
        return 0;
    }

    if (cmd == IPC_RMID) {
        unlink(seg->path);
        seg->removed = 1;
        return 0;
    }

    if (cmd == IPC_SET)
        return 0;

    ERRNO = EINVAL;
    return -1;
}