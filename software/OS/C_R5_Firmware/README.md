# C_R5_Firmware

The bare-metal Cortex-R5F mirror firmware. Loaded by remoteproc at boot,
polls the shared-memory mailbox and reflects flag bits onto AXI GPIO.

| File | Role |
|---|---|
| `rpmsg-echo.c`             | Top-level firmware: main, app, GPIO helpers, shm_poll_tick, the rpmsg endpoint callback, the kind parser. 304 lines. |
| `rpmsg-echo.h`             | Endpoint name constant `"rpmsg-openamp-demo-channel"`. |
| `shm_alert.h`              | Shared-memory contract: TCM_0B physical address, magic word, struct layout. |
| `platform_info.c`          | libmetal device descriptors, IPI base addresses, vring physical addresses. 285 lines. |
| `platform_info.h`          | Platform constants header. |
| `rsc_table.c`              | OpenAMP resource table. Read by the kernel remoteproc loader at firmware load time. |
| `rsc_table.h`              | Resource-table type declarations. |
| `zynqmp_r5_a53_rproc.c`    | Remoteproc operations table (init, mmap, notify, wait_for_done). |
| `helper.c`                 | libmetal log handler, optional UART setup, R5 PMU cycle reader. |
| `lscript.ld`               | Linker script. Defines DDR carve-out at 0x3ED00000 (256 KiB), OCM at 0xFFFF0000, TCM_0A at 0x00000000, TCM_0B at 0x00020000. |

## Build

In Vitis 2022.2: open `vitis/rpu/`, right-click the `rpu` project,
Build → Build Project. Output: `vitis/rpu/Debug/rpu.elf` (~96 KiB).

## Deploy

```sh
scp vitis/rpu/Debug/rpu.elf petalinux@192.168.1.78:/tmp/
ssh petalinux@192.168.1.78 << 'EOSSH'
sudo cp /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw \
        /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw.bak
sudo cp /tmp/rpu.elf /lib/firmware/rproc-ff9a0000.rf5ss:r5f_0-fw
echo stop  | sudo tee /sys/class/remoteproc/remoteproc0/state
echo start | sudo tee /sys/class/remoteproc/remoteproc0/state
EOSSH
```

The detailed walkthrough is at `../../../RPU_Code_Documentation.md`.
