/* JSONL line scanner for the native helper spike (#48) — ctypes surface.
 *
 * scan_file() mirrors hotmem JSONLInspector._stream semantics:
 *   - "row"      = non-blank line (bytes outside {space,\t,\r,\v,\f} exist)
 *   - blank lines do not count as rows, do not become first_bad, and do not
 *     consume a sample slot — but they DO advance the line index (exactly
 *     like _handle_line's `line_index >= sample_size` gate).
 *   - sample     = first lines with line_index < sample_max that satisfy the
 *                  mode's capture rule, recorded as (file offset, length
 *                  INCLUDING the trailing newline).
 *   - mode 0 (scan-only): capture non-blank lines; no JSON validation.
 *   - mode 1 (full):      validate every non-blank line; capture VALID lines;
 *                          report first non-blank INVALID line (index+offset).
 *   - the final line without a trailing newline counts like any other line
 *     (length excludes the absent newline).
 *
 * Line offsets use correct file coordinates. NOTE: the shipped Python
 * _stream overstates offsets for lines following a chunk-spanning line
 * (line_start = offset + pos in carry+chunk coords) — parity comparisons in
 * the spike account for this documented divergence. See README.
 *
 * Lines longer than 16 MiB are treated as invalid (spike policy; the corpus
 * uses ~1.2 KiB lines).
 *
 * Returns 0 on success, -1 open failure, -3 read failure.
 */
#ifndef HOTMEM_SPIKE_SCAN_H
#define HOTMEM_SPIKE_SCAN_H

#include <stdint.h>

#define SCAN_MAX_SAMPLES 16

typedef struct {
    uint64_t rows;             /* non-blank lines */
    int64_t first_bad_index;   /* -1 when none */
    uint64_t first_bad_offset; /* valid when first_bad_index >= 0 */
    uint64_t lines;            /* total completed lines (blank included) */
    uint32_t sample_count;
    uint64_t sample_offsets[SCAN_MAX_SAMPLES];
    uint64_t sample_lengths[SCAN_MAX_SAMPLES];
} scan_result;

int scan_file(const char *path, uint32_t sample_max, int mode, scan_result *out);

#endif
