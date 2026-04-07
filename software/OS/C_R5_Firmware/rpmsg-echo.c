/* rpmsg-echo.c - first attempt at the LED actuation path over OpenAMP RPMsg.
 * Will be replaced with a TCM mailbox fast path after the RPMsg latency
 * measurement comes back at ~17 ms one-way (too slow for the actuation
 * NFR of <100 ms end-to-end including network + plug-in + IPC).
 */
#include "rpmsg-echo.h"

/* TODO: poll rpmsg endpoint for an alert message and toggle GPIO */
