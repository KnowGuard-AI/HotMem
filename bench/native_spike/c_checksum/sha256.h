/* Self-contained SHA-256 for the native helper spike (#48).
 *
 * Standard FIPS 180-4 implementation (public-domain style, no external
 * dependencies): the local box has gcc but no OpenSSL headers, and the
 * spike must measure native hashing end-to-end without assuming libcrypto.
 */
#ifndef HOTMEM_SPIKE_SHA256_H
#define HOTMEM_SPIKE_SHA256_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t buffer[64];
    size_t buflen;
} sha256_ctx;

void sha256_init(sha256_ctx *ctx);
void sha256_update(sha256_ctx *ctx, const uint8_t *data, size_t len);
void sha256_final(sha256_ctx *ctx, uint8_t out[32]);
void sha256_hex(const uint8_t digest[32], char out_hex[65]);

#endif
