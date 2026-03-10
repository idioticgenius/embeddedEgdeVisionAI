# Vivado_Hardware

The Vivado hardware design source for the kv260_vcuDecode_vmixDP base
platform — the production hardware that hosts the multichannel-openamp-gpio
overlay.

```
Vivado_Hardware/
└── kv260_vcuDecode_vmixDP/
    ├── Makefile           # builds the Vivado project from scripts
    ├── scripts/
    │   ├── main.tcl       # top-level project script (create_project, add IPs)
    │   ├── config_bd.tcl  # block-design construction (instantiates VCU, v_mix, DP)
    │   └── *.tcl          # supporting Tcl scripts
    ├── xdc/
    │   └── pin.xdc        # pin constraints for KV260 carrier
    └── ip/                # custom IP cores (HLS-generated, with sources)
```

## Build the bitstream

```sh
cd Vivado_Hardware/kv260_vcuDecode_vmixDP
make            # invokes vivado -source scripts/main.tcl
                # synthesis ~22 min, place-and-route ~95 min
```

Output: `kv260_vcuDecode_vmixDP_wrapper.bit` and the `.xsa` exported via
`write_hw_platform`.

## Notes

- The project sits within `kria-vitis-platforms/kv260/platforms/vivado/`.
- The `.xsa` consumed by Vitis is the pre-synthesis hardware export.
- The four other Vivado projects (`kv260_ispMipiRx_*`) in the upstream
  repo are alternative platforms not used by this project.
