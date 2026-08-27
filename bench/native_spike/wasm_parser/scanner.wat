;; WebAssembly line scanner for the native helper spike (#48).
;;
;; Scan-only contract (matches py_baseline.py_scan_only and the C scanner's
;; mode 0): count non-blank lines, capture the first N line boundaries
;; (file offset, length INCLUDING trailing newline), where eligibility is
;; line_index < sample_max and blank lines burn line_index but never
;; produce a sample. The final line without a trailing newline counts with
;; length excluding the absent newline.
;;
;; Architecture mirrors the real integration shape: the HOST (Python) owns
;; file I/O and carry management — it prepends leftover bytes to the next
;; chunk so every scan() call starts exactly at a line boundary — and the
;; module does the per-byte CPU work over linear memory.
;;
;; Layout: sample table at 0x1000 (16 slots of i64 offset/length pairs,
;; 256 bytes); host writes (carry + chunk) at 0x2000 upward — up to ~1 MiB
;; of fresh chunk bytes plus carried partial-line bytes, with the host
;; growing memory when long lines need more (initial 33 pages = 2.125 MiB).
;;
;; Offsets use correct file coordinates (see py_baseline.py note about the
;; shipped _stream offset bug).
(module
  (memory (export "memory") 33)

  (global $rows (mut i64) (i64.const 0))
  (global $lines (mut i64) (i64.const 0))
  (global $sample_count (mut i32) (i32.const 0))
  (global $sample_max (mut i32) (i32.const 5))
  (global $pend_start (mut i64) (i64.const 0))
  (global $pend_nonblank (mut i32) (i32.const 0))
  (global $have_pend (mut i32) (i32.const 0))

  ;; true when byte is line whitespace: space, \t, \r, \v, \f (not \n)
  (func $ws (param $b i32) (result i32)
    (i32.or (i32.eq (local.get $b) (i32.const 32))
      (i32.or (i32.eq (local.get $b) (i32.const 9))
        (i32.or (i32.eq (local.get $b) (i32.const 13))
          (i32.or (i32.eq (local.get $b) (i32.const 11))
            (i32.eq (local.get $b) (i32.const 12)))))))

  (func $eligible (result i32)
    (i32.and
      (i64.lt_u (global.get $lines) (i64.extend_i32_u (global.get $sample_max)))
      (i32.lt_u (global.get $sample_count) (global.get $sample_max))))

  (func $record_sample (param $start i64) (param $len i64)
    (if (i32.lt_u (global.get $sample_count) (i32.const 16))
      (then
        (i64.store
          (i32.add (i32.const 0x1000) (i32.mul (global.get $sample_count) (i32.const 16)))
          (local.get $start))
        (i64.store
          (i32.add (i32.const 0x1008) (i32.mul (global.get $sample_count) (i32.const 16)))
          (local.get $len))
        (global.set $sample_count (i32.add (global.get $sample_count) (i32.const 1))))))

  ;; Scan bytes at [ptr, ptr+len), which live at absolute file offset `abs`.
  ;; The first byte is always a line boundary (host carry guarantee).
  (func (export "scan") (param $ptr i32) (param $len i32) (param $abs i64)
    (local $i i32)
    (local $b i32)
    (local $nl_abs i64)
    (if (i32.eqz (global.get $have_pend))
      (then
        (global.set $have_pend (i32.const 1))
        (global.set $pend_start (local.get $abs))
        (global.set $pend_nonblank (i32.const 0))))
    (loop $bytes
      (if (i32.lt_u (local.get $i) (local.get $len))
        (then
          (local.set $b (i32.load8_u (i32.add (local.get $ptr) (local.get $i))))
          (if (i32.eq (local.get $b) (i32.const 10))
            (then
              (local.set $nl_abs
                (i64.add (local.get $abs) (i64.extend_i32_u (local.get $i))))
              (if (global.get $pend_nonblank)
                (then
                  (global.set $rows (i64.add (global.get $rows) (i64.const 1)))
                  (if (call $eligible)
                    (then
                      (call $record_sample
                        (global.get $pend_start)
                        (i64.add (i64.sub (local.get $nl_abs) (global.get $pend_start))
                                 (i64.const 1)))))))
              (global.set $lines (i64.add (global.get $lines) (i64.const 1)))
              (global.set $pend_start (i64.add (local.get $nl_abs) (i64.const 1)))
              (global.set $pend_nonblank (i32.const 0)))
            (else
              (if (i32.eqz (call $ws (local.get $b)))
                (then (global.set $pend_nonblank (i32.const 1))))))
          (local.set $i (i32.add (local.get $i) (i32.const 1)))
          (br $bytes)))))

  ;; Finalize with the total file size: the last line may lack a newline.
  (func (export "finish") (param $total i64)
    (if (i32.and (global.get $have_pend) (global.get $pend_nonblank))
      (then
        (global.set $rows (i64.add (global.get $rows) (i64.const 1)))
        (if (call $eligible)
          (then
            (call $record_sample
              (global.get $pend_start)
              (i64.sub (local.get $total) (global.get $pend_start))))))))

  (func (export "reset")
    (global.set $rows (i64.const 0))
    (global.set $lines (i64.const 0))
    (global.set $sample_count (i32.const 0))
    (global.set $pend_nonblank (i32.const 0))
    (global.set $have_pend (i32.const 0))
    (global.set $pend_start (i64.const 0)))

  (func (export "set_sample_max") (param $n i32)
    (global.set $sample_max (local.get $n)))

  (func (export "get_rows") (result i64) (global.get $rows))
  (func (export "get_lines") (result i64) (global.get $lines))
  (func (export "get_sample_count") (result i32) (global.get $sample_count))
  (func (export "get_sample_offset") (param $i i32) (result i64)
    (i64.load (i32.add (i32.const 0x1000) (i32.mul (local.get $i) (i32.const 16)))))
  (func (export "get_sample_length") (param $i i32) (result i64)
    (i64.load (i32.add (i32.const 0x1008) (i32.mul (local.get $i) (i32.const 16))))))
