/* Range SHA-256 hashing for the native helper spike (#48) — ctypes surface.
 *
 * Two variants over the byte range [offset, offset+length):
 *   range_hash_pread — open + pread loop over a 1 MiB stack-static buffer
 *                      (O(1 MiB) memory, mirrors the Python streaming shape).
 *   range_hash_mmap  — mmap exactly the page-aligned range window and hash
 *                      in place (zero-copy; resident pages = touched pages).
 *
 * Return codes:
 *    0  success (out_hex holds 64 hex chars + NUL)
 *   -1  cannot open path
 *   -2  truncated (file shorter than offset+length)
 *   -3  I/O or mmap failure (message in err if provided)
 */
#define _POSIX_C_SOURCE 200809L

#include "sha256.h"

#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define READ_BUF (1 << 20)

static uint8_t g_buf[READ_BUF];

static int finish_hex(sha256_ctx *ctx, char out_hex[65]) {
    uint8_t digest[32];

    sha256_final(ctx, digest);
    sha256_hex(digest, out_hex);
    return 0;
}

int range_hash_pread(const char *path, uint64_t offset, uint64_t length, char out_hex[65]) {
    int fd = open(path, O_RDONLY);
    sha256_ctx ctx;
    uint64_t pos = offset, remaining = length;

    if (fd < 0) {
        return -1;
    }
    sha256_init(&ctx);
    while (remaining > 0) {
        size_t want = remaining < READ_BUF ? (size_t)remaining : (size_t)READ_BUF;
        ssize_t got = pread(fd, g_buf, want, (off_t)pos);
        if (got < 0) {
            close(fd);
            return -3;
        }
        if (got == 0) {
            close(fd);
            return -2;
        }
        sha256_update(&ctx, g_buf, (size_t)got);
        pos += (uint64_t)got;
        remaining -= (uint64_t)got;
    }
    close(fd);
    return finish_hex(&ctx, out_hex);
}

int range_hash_mmap(const char *path, uint64_t offset, uint64_t length, char out_hex[65]) {
    int fd = open(path, O_RDONLY);
    struct stat st;
    sha256_ctx ctx;
    long page;
    uint64_t map_off, map_len, delta;
    const uint8_t *map;

    if (fd < 0) {
        return -1;
    }
    if (fstat(fd, &st) != 0) {
        close(fd);
        return -3;
    }
    if (length == 0) {
        close(fd);
        sha256_init(&ctx);
        return finish_hex(&ctx, out_hex);
    }
    if (st.st_size < 0 || (uint64_t)st.st_size < offset || (uint64_t)st.st_size - offset < length) {
        close(fd);
        return -2;
    }

    page = sysconf(_SC_PAGESIZE);
    map_off = (offset / (uint64_t)page) * (uint64_t)page;
    delta = offset - map_off;
    map_len = delta + length;

    map = (const uint8_t *)mmap(NULL, (size_t)map_len, PROT_READ, MAP_PRIVATE, fd, (off_t)map_off);
    if (map == MAP_FAILED) {
        close(fd);
        return -3;
    }

    sha256_init(&ctx);
    sha256_update(&ctx, map + delta, (size_t)length);
    munmap((void *)map, (size_t)map_len);
    close(fd);
    return finish_hex(&ctx, out_hex);
}
