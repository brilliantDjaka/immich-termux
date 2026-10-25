/*
 * shm_test.c -- self-test for shm_compat.c (SysV shared memory emulation).
 *
 * Build (Termux clang):
 *   clang -O2 -o shm_test shm_test.c
 * Run:
 *   LD_PRELOAD=/path/to/libshm-compat.so ./shm_test
 *   ./shm_test          # control run: should fail or be killed by seccomp/SIGSYS
 *
 * Checks: create, attach, write, fork-shared memory, IPC_STAT fields, IPC_EXCL,
 * attach-by-key from a second process, detach, IPC_RMID.
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <sys/wait.h>
#include <unistd.h>

/* Termux's <sys/shm.h> maps the SysV calls onto libandroid-shmem symbols; we want to
 * call the plain names so that our own libshm-compat.so can provide them. */
#ifdef shmget
#undef shmget
#endif
#ifdef shmat
#undef shmat
#endif
#ifdef shmdt
#undef shmdt
#endif
#ifdef shmctl
#undef shmctl
#endif
int shmget(int key, size_t size, int shmflg);
void *shmat(int shmid, const void *shmaddr, int shmflg);
int shmdt(const void *shmaddr);
int shmctl(int shmid, int cmd, void *buf);

#define SEG_KEY 0x5e9f1234
#define SEG_SIZE 8192

static int failures;

static void check(int condition, const char *what)
{
    printf("%-52s %s\n", what, condition ? "ok" : "FAIL");
    if (!condition)
        failures++;
}

int main(void)
{
    int id = shmget((key_t)SEG_KEY, SEG_SIZE, IPC_CREAT | IPC_EXCL | 0600);
    if (id < 0) {
        printf("shmget failed: %s (errno %d) -- SysV shm unavailable\n", strerror(errno), errno);
        return 2;
    }
    check(1, "shmget(IPC_CREAT|IPC_EXCL)");

    int dup_excl = shmget((key_t)SEG_KEY, SEG_SIZE, IPC_CREAT | IPC_EXCL | 0600);
    check(dup_excl < 0 && errno == EEXIST, "second IPC_EXCL create -> EEXIST");

    char *mem = shmat(id, NULL, 0);
    check(mem != (char *)-1, "shmat");
    strcpy(mem, "parent");
    mem[4096] = 'P';

    struct shmid_ds ds;
    memset(&ds, 0, sizeof(ds));
    check(shmctl(id, IPC_STAT, &ds) == 0, "shmctl(IPC_STAT)");
    check(ds.shm_perm.key == SEG_KEY, "  shm_perm.key matches");
    check(ds.shm_perm.mode == 0600, "  shm_perm.mode == 0600");
    check(ds.shm_segsz == SEG_SIZE, "  shm_segsz == 8192");
    check(ds.shm_nattch >= 1, "  shm_nattch >= 1");

    pid_t pid = fork();
    if (pid == 0) {
        /* different process, attached by key (no IPC_CREAT) */
        int id2 = shmget((key_t)SEG_KEY, SEG_SIZE, 0);
        if (id2 < 0)
            _exit(10);
        char *m2 = shmat(id2, NULL, 0);
        if (m2 == (char *)-1)
            _exit(11);
        if (strcmp(m2, "parent") != 0)
            _exit(12);
        strcpy(m2 + 64, "child");
        m2[4096] = 'C';
        _exit(0);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    check(WIFEXITED(status) && WEXITSTATUS(status) == 0, "child attached by key and saw parent data");
    check(strcmp(mem + 64, "child") == 0, "parent sees child's write (memory is shared)");
    check(mem[4096] == 'C', "second page shared too");

    check(shmdt(mem) == 0, "shmdt");
    check(shmctl(id, IPC_RMID, NULL) == 0, "shmctl(IPC_RMID)");

    printf("\n%s (%d failure(s))\n", failures ? "FAILED" : "PASSED", failures);
    return failures ? 1 : 0;
}