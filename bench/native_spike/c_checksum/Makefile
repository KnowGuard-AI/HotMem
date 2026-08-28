# Native helper spike (#48) — C range-checksum candidate.
#
# Self-contained: no OpenSSL/libcrypto dependency (the target box has no
# OpenSSL headers). Emits librange_hash.so for the ctypes loader.

CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11 -fPIC

LIB := librange_hash.so
OBJS := sha256.o range_hash.o

all: $(LIB)

$(LIB): $(OBJS)
	$(CC) $(CFLAGS) -shared -o $@ $(OBJS)

%.o: %.c sha256.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(LIB) $(OBJS)

.PHONY: all clean
