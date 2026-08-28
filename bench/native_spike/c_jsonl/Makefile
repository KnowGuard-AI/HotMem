# Native helper spike (#48) — C JSONL scanner candidate.
# Emits libscan.so for the ctypes loader.

CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=c11 -fPIC

LIB := libscan.so
OBJS := scan.o

all: $(LIB)

$(LIB): $(OBJS)
	$(CC) $(CFLAGS) -shared -o $@ $(OBJS)

%.o: %.c scan.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(LIB) $(OBJS)

.PHONY: all clean
