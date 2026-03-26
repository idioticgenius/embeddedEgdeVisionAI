/*
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * APU↔R5 shared flag page for the zone-alert LED fast path.
 *
 * Both the APU-side zoneguard GStreamer plugin and the R5 baremetal
 * firmware include this header so there is exactly one definition of
 * the layout.
 *
 * Memory: 4 KiB at the start of TCM_0B (on-chip SRAM tightly-coupled
 * to the R5_0 core). APU maps it via /dev/mem with O_SYNC and sees
 * the global TCM_0B alias. R5 accesses the same memory through the
 * same global address; the default R5 standalone BSP translation
 * table maps 0xFFE00000-0xFFE3FFFF as Normal non-cacheable memory,
 * so no cache maintenance is required on the R5 side.
 *
 * Protocol: APU writes `flags` (bit i = channel i alert active),
 * increments `seq`, optionally timestamps `ts_ns`. R5 polls `flags`
 * in a tight loop and mirrors the low 4 bits onto axi_gpio_0 data
 * register. The first writer on APU side writes `magic` once to
 * SHM_ALERT_MAGIC — R5 skips the poll if magic doesn't match so
 * transient boot-order races don't cause spurious LED activity.
 */

#ifndef SHM_ALERT_H
#define SHM_ALERT_H

#include <stdint.h>

/*
 * The flag page lives at the start of TCM_0B at 0xFFE20000.
 *
 * History of target-region choice:
 *   1. DDR tail of rpu0vdev0buffer (0x3EE47000) — rejected: the DT
 *      marks it `no-map`, so APU /dev/mem mmap SIGBUSes on access
 *      even with CONFIG_STRICT_DEVMEM=n.
 *   2. OCM bank 2 (0xFFFE0000) — APU access worked, but R5 appears
 *      to fault on first access from shm_poll_tick (main loop stops
 *      advancing after one iteration; rpmsg IRQ path still works).
 *      Likely cause: OCM region not covered by the R5 standalone
 *      BSP default MPU, or cache maintenance ops fault on that
 *      address range.
 *   3. TCM_0B (0xFFE20000) — CURRENT CHOICE. TCM is on-chip SRAM
 *      belonging to R5_0 in split mode, always mapped in the R5's
 *      default translation table as Normal non-cacheable memory,
 *      and reachable from the APU at the same global address via
 *      the TCM slave port. No cache maintenance needed on either
 *      side; access latency is deterministic.
 */
#define SHM_ALERT_PA     0xFFE20000UL  /* start of TCM_0B, first 4 KiB */
#define SHM_ALERT_SIZE   0x1000UL      /* 4 KB */
#define SHM_ALERT_MAGIC  0x5A4C4544UL  /* "ZLED" (LE: 'Z','L','E','D') */

struct shm_alert {
    uint32_t magic;     /* SHM_ALERT_MAGIC — init sentinel */
    uint32_t flags;     /* bit i = channel i alert active (i in 0..3) */
    uint32_t seq;       /* incremented on every flag-write (RTT/liveness) */
    uint32_t reserved;
    uint64_t ts_ns;     /* APU CLOCK_MONOTONIC_RAW ns at last write */
};

#endif /* SHM_ALERT_H */
