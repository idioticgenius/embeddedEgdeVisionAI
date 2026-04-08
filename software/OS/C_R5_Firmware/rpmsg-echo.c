/* rpmsg-echo.c - tight TCM mailbox poll loop.
 *
 * Replaces the OpenAMP RPMsg attempt: RPMsg measured ~17 ms one-way,
 * unworkable for the actuation hot path. Mailbox at 0xFFE20000 with a
 * magic-value handshake. Dcache invalidated each iteration.
 */
#include <xil_io.h>
#include <xil_cache.h>
#include "shm_alert.h"

extern void XGpio_Init(void);
extern void XGpio_DiscreteWrite(unsigned channel, unsigned mask);

void mailbox_poll_forever(void)
{
    volatile shm_alert_t *page = (shm_alert_t *) SHM_ALERT_ADDR;
    unsigned last = 0;
    XGpio_Init();
    for (;;) {
        Xil_DCacheInvalidateRange((INTPTR)page, sizeof(*page));
        if (page->magic == SHM_ALERT_MAGIC && page->seq != last) {
            XGpio_DiscreteWrite(1, page->led_mask);
            last = page->seq;
        }
    }
}
