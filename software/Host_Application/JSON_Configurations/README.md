# JSON_Configurations

The six VVAS pipeline JSON configurations referenced by the `MODELS` dict
in `server.py` (lines 212–231 of the latest `latest-project-files/code/server.py`).
Each model definition pairs one `infer_json` (consumed by `vvas_xinfer`)
with one `meta_json` (consumed by `vvas_xmetaconvert`). Three models are
defined; two are exposed to the operator dashboard, the third is defined
but not currently exposed in the UI.

## Mapping from `server.py:MODELS` to the JSON files

| Model key (server.py)  | UI label                       | `infer_json`                | `meta_json`                       | Input  | Notes |
|---|---|---|---|---|---|
| `refinedet`            | Person Detect (RefineDet)      | `kernel_refinedet.json`     | `metaconvert_config.json`         | 480×360 BGR | Active production person detector. |
| `densebox`             | Face Detect (DenseBox)         | `kernel_densebox.json`      | `metaconvert_facedetect.json`     | 640×360 BGR | Active production face detector. Runs on RefineDet ROIs in cascade mode. |
| `ssd_mobilenet`        | Object Detect (SSD MobileNet)  | `kernel_ssd_mobilenet.json` | `metaconvert_ssd_person.json`     | 300×300 RGB | Defined in MODELS but not currently exposed in the dashboard. |

## How `server.py` consumes the files

When the operator selects a model in the dashboard, `server.py` reads the
corresponding entry from the `MODELS` dict and interpolates the absolute
paths into the gst-launch-1.0 pipeline string. The principal call shape is:

```
vvas_xinfer infer-config-location=/home/petalinux/jsons/kernel_refinedet.json
vvas_xmetaconvert config-location=/home/petalinux/jsons/metaconvert_config.json
```

`server.py` also writes a per-pipeline `args.json` at run time that holds
the full set of vvas_xinfer arguments for the active pipeline. That file
is regenerated on every `pipeline.start()` call from the operator UI and
is therefore not a source file.

## Why exactly these six

The `MODELS` dict at `server.py:212–231` defines three models, each with a
matched pair of JSON files. Three models × two JSONs = six files. No other
JSONs in `/home/petalinux/jsons/` are referenced by `server.py`. The other
14 JSONs in the board snapshot are alternate or experimental configurations
(cascade variants, pre-processor flavours, the tracker config, the bbox
post-processor, the fixed-point image-processor) that are kept in the
snapshot for future work but are not part of the production pipeline.

The full set of unused JSONs is preserved on the board at
`/home/petalinux/jsons/` and in the upstream snapshot at
`../../../latest-project-files/jsons/` for reference.

## Format reference

Each file is a JSON object that the relevant VVAS GStreamer element parses
at pipeline construction time. The schema is documented in AMD UG1354
(Vitis Video Analytics SDK User Guide).
