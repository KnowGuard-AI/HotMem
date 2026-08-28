/* JSONL line scanner + strict JSON validator (see scan.h). */
#include "scan.h"

#define _POSIX_C_SOURCE 200809L

#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define READ_CHUNK (1 << 20)
#define MAX_LINE (16 << 20)
#define MAX_DEPTH 200

/* ------------------------------------------------------------------ */
/* Strict JSON validator (RFC 8259 minus NaN/Infinity, which Python's  */
/* json module accepts; the corpus contains neither).                  */
/* ------------------------------------------------------------------ */

typedef struct {
    const uint8_t *p;
    const uint8_t *end;
    int depth;
} jval;

static void j_ws(jval *j) {
    while (j->p < j->end) {
        uint8_t c = *j->p;
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
            j->p++;
        } else {
            break;
        }
    }
}

static int j_hex(uint8_t c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

static int j_string(jval *j) {
    j->p++; /* opening quote */
    while (j->p < j->end) {
        uint8_t c = *j->p;
        if (c == '"') {
            j->p++;
            return 1;
        }
        if (c == '\\') {
            j->p++;
            if (j->p >= j->end) {
                return 0;
            }
            uint8_t e = *j->p;
            if (e == '"' || e == '\\' || e == '/' || e == 'b' || e == 'f' || e == 'n' ||
                e == 'r' || e == 't') {
                j->p++;
            } else if (e == 'u') {
                j->p++;
                for (int i = 0; i < 4; i++, j->p++) {
                    if (j->p >= j->end || !j_hex(*j->p)) {
                        return 0;
                    }
                }
            } else {
                return 0;
            }
        } else if (c < 0x20) {
            return 0;
        } else {
            j->p++;
        }
    }
    return 0;
}

static int j_digits(jval *j) {
    const uint8_t *start = j->p;
    while (j->p < j->end && *j->p >= '0' && *j->p <= '9') {
        j->p++;
    }
    return j->p > start;
}

static int j_number(jval *j) {
    if (j->p < j->end && *j->p == '-') {
        j->p++;
    }
    if (j->p < j->end && *j->p == '0') {
        j->p++;
    } else if (j->p < j->end && *j->p >= '1' && *j->p <= '9') {
        j_digits(j);
    } else {
        return 0;
    }
    if (j->p < j->end && *j->p == '.') {
        j->p++;
        if (!j_digits(j)) {
            return 0;
        }
    }
    if (j->p < j->end && (*j->p == 'e' || *j->p == 'E')) {
        j->p++;
        if (j->p < j->end && (*j->p == '+' || *j->p == '-')) {
            j->p++;
        }
        if (!j_digits(j)) {
            return 0;
        }
    }
    return 1;
}

static int j_lit(jval *j, const char *lit, size_t n) {
    if ((size_t)(j->end - j->p) < n || memcmp(j->p, lit, n) != 0) {
        return 0;
    }
    j->p += n;
    return 1;
}

static int j_value(jval *j) {
    uint8_t c;

    if (j->depth > MAX_DEPTH) {
        return 0;
    }
    j_ws(j);
    if (j->p >= j->end) {
        return 0;
    }
    c = *j->p;
    if (c == '"') {
        return j_string(j);
    }
    if (c == '{') {
        j->p++;
        j->depth++;
        j_ws(j);
        if (j->p < j->end && *j->p == '}') {
            j->p++;
            j->depth--;
            return 1;
        }
        for (;;) {
            j_ws(j);
            if (j->p >= j->end || *j->p != '"') {
                j->depth--;
                return 0;
            }
            if (!j_string(j)) {
                j->depth--;
                return 0;
            }
            j_ws(j);
            if (j->p >= j->end || *j->p != ':') {
                j->depth--;
                return 0;
            }
            j->p++;
            if (!j_value(j)) {
                j->depth--;
                return 0;
            }
            j_ws(j);
            if (j->p < j->end && *j->p == ',') {
                j->p++;
                continue;
            }
            if (j->p < j->end && *j->p == '}') {
                j->p++;
                j->depth--;
                return 1;
            }
            j->depth--;
            return 0;
        }
    }
    if (c == '[') {
        j->p++;
        j->depth++;
        j_ws(j);
        if (j->p < j->end && *j->p == ']') {
            j->p++;
            j->depth--;
            return 1;
        }
        for (;;) {
            if (!j_value(j)) {
                j->depth--;
                return 0;
            }
            j_ws(j);
            if (j->p < j->end && *j->p == ',') {
                j->p++;
                continue;
            }
            if (j->p < j->end && *j->p == ']') {
                j->p++;
                j->depth--;
                return 1;
            }
            j->depth--;
            return 0;
        }
    }
    if (c == 't') {
        return j_lit(j, "true", 4);
    }
    if (c == 'f') {
        return j_lit(j, "false", 5);
    }
    if (c == 'n') {
        return j_lit(j, "null", 4);
    }
    return j_number(j);
}

static int json_valid(const uint8_t *data, size_t len) {
    jval j;

    j.p = data;
    j.end = data + len;
    j.depth = 0;
    if (!j_value(&j)) {
        return 0;
    }
    j_ws(&j);
    return j.p == j.end;
}

/* ------------------------------------------------------------------ */
/* Line scanner                                                        */
/* ------------------------------------------------------------------ */

static int is_ws(uint8_t c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\v' || c == '\f';
}

static int span_nonblank(const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        if (!is_ws(data[i])) {
            return 1;
        }
    }
    return 0;
}

static uint8_t g_chunk[READ_CHUNK];
static uint8_t *g_pend = NULL;     /* line assembly buffer */
static size_t g_pend_cap = 0;

static int pend_reserve(size_t need) {
    if (need <= g_pend_cap) {
        return 1;
    }
    size_t cap = g_pend_cap ? g_pend_cap : 4096;
    while (cap < need) {
        cap *= 2;
    }
    uint8_t *p = (uint8_t *)realloc(g_pend, cap);
    if (!p) {
        return 0;
    }
    g_pend = p;
    g_pend_cap = cap;
    return 1;
}

static void process_line(
    scan_result *res,
    const uint8_t *line,   /* contiguous, WITHOUT trailing newline */
    size_t len,
    uint64_t file_start,
    uint64_t len_incl_nl,  /* line length including the newline, when present */
    uint32_t sample_max,
    int mode               /* 0 scan-only, 1 full */
) {
    int nonblank = span_nonblank(line, len);
    int valid = 1;

    if (!nonblank) {
        return; /* blank: not a row, never bad, never sampled */
    }
    res->rows++;
    if (mode == 1) {
        valid = len <= MAX_LINE ? json_valid(line, len) : 0;
        if (!valid && res->first_bad_index < 0) {
            res->first_bad_index = (int64_t)res->lines;
            res->first_bad_offset = file_start;
        }
    }
    if (res->lines < sample_max && res->sample_count < SCAN_MAX_SAMPLES) {
        if (mode == 0 || valid) {
            res->sample_offsets[res->sample_count] = file_start;
            res->sample_lengths[res->sample_count] = len_incl_nl;
            res->sample_count++;
        }
    }
}

int scan_file(const char *path, uint32_t sample_max, int mode, scan_result *out) {
    int fd = open(path, O_RDONLY);
    uint64_t chunk_start = 0;
    size_t pend_len = 0;

    if (fd < 0) {
        return -1;
    }
    memset(out, 0, sizeof *out);
    out->first_bad_index = -1;
    if (!pend_reserve(4096)) {
        close(fd);
        return -3;
    }

    for (;;) {
        ssize_t got = read(fd, g_chunk, READ_CHUNK);
        if (got < 0) {
            close(fd);
            return -3;
        }
        if (got == 0) {
            break;
        }
        size_t len = (size_t)got;
        size_t pos = 0;
        for (;;) {
            const uint8_t *nl = (const uint8_t *)memchr(g_chunk + pos, '\n', len - pos);
            if (!nl) {
                break;
            }
            size_t k = (size_t)(nl - g_chunk);      /* newline position in chunk */
            size_t prefix = k - pos;                /* chunk bytes of this line */
            /* invariant: pend_len > 0 implies pos == 0 (line continues chunk head) */
            if (!pend_reserve(pend_len + prefix)) {
                close(fd);
                return -3;
            }
            memcpy(g_pend + pend_len, g_chunk + pos, prefix);
            size_t line_len = pend_len + prefix;
            uint64_t file_start = chunk_start + pos - pend_len;
            process_line(out, g_pend, line_len, file_start, line_len + 1, sample_max, mode);
            out->lines++;
            pend_len = 0;
            pos = k + 1;
        }
        if (pos < len) {
            if (!pend_reserve(pend_len + (len - pos))) {
                close(fd);
                return -3;
            }
            memcpy(g_pend + pend_len, g_chunk + pos, len - pos);
            pend_len += len - pos;
        }
        chunk_start += len;
    }
    close(fd);

    if (pend_len > 0) {
        uint64_t file_start = chunk_start - pend_len;
        process_line(out, g_pend, pend_len, file_start, pend_len, sample_max, mode);
        out->lines++;
    }
    return 0;
}
