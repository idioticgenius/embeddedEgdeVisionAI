# Software

Host and target software.

* `OS/PetaLinux_BSP/` - the PetaLinux 2022.2 BSP project. Only the user-edited
  parts under `project-spec/meta-user/` and the project configs are tracked
  here. The vendor Yocto layers come from the standard 2022.2 release.
* `OS/C_R5_Firmware/` - bare-metal C firmware for the Cortex-R5. Includes the
  TCM mailbox fast-path implementation.
* `Host_Application/Python_Flask/` - the Flask operator dashboard
  (`server.py`), the APU-side mailbox bridge (`rpu_bridge.py`) and the latency
  capture / test scripts.
* `Host_Application/C_GStreamer_Plugin/` - the `zoneguard` GStreamer plug-in
  written in C against `GstBaseTransform`. Implements the rectangle hit-test
  and the three-frame hysteresis state machine.
* `Host_Application/HTML_CSS_JS_Frontend/` - vanilla HTML / CSS / JS dashboard
  and architecture diagrams.
* `Host_Application/JSON_Configurations/` - VVAS kernel and metaconvert
  configurations for the four-channel pipeline.
* `Host_Application/Shell_Scripts/` - bench scripts: pipeline launch, soak
  runs, statistics capture, firmware install.
* `Host_Application/Logs/` - captured boot, latency and soak logs (evaluation
  evidence).
