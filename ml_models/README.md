# ML_Models_Vitis_AI

The two production neural-network models compiled for the DPUCZDX8G B3136
on the Kria KV260, plus the DPU architecture descriptor and the v++ link
configurations needed to reproduce the build.

## Layout

```
ML_Models_Vitis_AI/
├── README.md                              # this file
├── arch_dpu_kv260.json                    # DPU arch descriptor for vai_c_caffe
├── shell.json                             # xmutil shell manifest
├── image_processing.cfg                   # v++ link config for the image-processing kernel
└── xmodel_deployable/                     # production-ready model bundles
    ├── refinedet_pruned_0_96/             # active person detector
    ├── refinedet_pruned_0_96_acc/         # accuracy-tuned variant (symlinks to the above .xmodel)
    └── densebox_640_360/                  # active face detector
```

## Production models

### RefineDet (Person Detect) — `refinedet_pruned_0_96`

| Property | Value |
|---|---|
| Model name           | `refinedet_pruned_0_96` |
| Internal kernel      | `refinedet_480x360_5G` |
| Model class          | `REFINEDET` |
| Number of classes    | 2 (background + person) |
| Input                | 480 × 360, BGR |
| Mean (B, G, R)       | 104, 117, 123 |
| Scale                | 1.0 |
| Compute              | 5.08 GOPs (pruned to 0.96) |
| DPU target           | DPUCZDX8G_ISA1_B3136 |
| Board file path      | `/usr/share/vitis_ai_library/models/refinedet_pruned_0_96/refinedet_pruned_0_96.xmodel` |
| Vitis AI Model Zoo ID | `cf_refinedet_coco_480_360_0.96_5.08G_2.5` (Caffe, COCO person subset) |

Files included:
- `xmodel_deployable/refinedet_pruned_0_96/refinedet_pruned_0_96.xmodel` — compiled INT8 model, 924,479 bytes.
- `xmodel_deployable/refinedet_pruned_0_96/refinedet_pruned_0_96.prototxt` — model card with mean/scale, NMS thresholds, anchor priors.
- `xmodel_deployable/refinedet_pruned_0_96/meta.json` — runtime metadata (kernel name `subgraph_Elt3`, library `libvart-dpu-runner.so`).
- `xmodel_deployable/refinedet_pruned_0_96/md5sum.txt` — checksum verification file.
- `xmodel_deployable/refinedet_pruned_0_96_acc/` — accuracy-tuned prototxt variant; `.xmodel` is a symlink to the main bundle.

### DenseBox (Face Detect) — `densebox_640_360`

| Property | Value |
|---|---|
| Model name           | `densebox_640_360` |
| Internal kernel      | `tiling_v7_640` |
| Model class          | `DENSE_BOX` / `FACEDETECT` |
| Number of classes    | 2 |
| Input                | 640 × 360, BGR |
| Mean (B, G, R)       | 128, 128, 128 |
| Scale                | 1.0 |
| Compute              | 1.11 GOPs |
| DPU target           | DPUCZDX8G_ISA1_B3136 |
| Board file path      | `/usr/share/vitis_ai_library/models/densebox_640_360/densebox_640_360.xmodel` |
| Vitis AI Model Zoo ID | `cf_densebox_wider_640_360_1.11G_2.5` (Caffe, WIDER FACE dataset) |

Files included:
- `xmodel_deployable/densebox_640_360/densebox_640_360.xmodel` — compiled INT8 model, 926,688 bytes.
- `xmodel_deployable/densebox_640_360/densebox_640_360.prototxt` — model card with mean/scale, NMS threshold 0.3, detection threshold 0.9.
- `xmodel_deployable/densebox_640_360/meta.json` — runtime metadata (kernel `subgraph_L0`, library `libvart-dpu-runner.so`).
- `xmodel_deployable/densebox_640_360/md5sum.txt` — checksum verification file.

## Provenance

Both bundles are downloaded directly from the Vitis AI Model Zoo open-download
portal at the following URLs:

```
densebox_640_360-kv260_DPUCZDX8G_ISA1_B3136-r2.5.0.tar.gz
   https://www.xilinx.com/bin/public/openDownload?filename=densebox_640_360-kv260_DPUCZDX8G_ISA1_B3136-r2.5.0.tar.gz
   md5: 21e8e3644be9e7f638ae2d660582ef99

refinedet_pruned_0_96-kv260_DPUCZDX8G_ISA1_B3136-r2.5.0.tar.gz
   https://www.xilinx.com/bin/public/openDownload?filename=refinedet_pruned_0_96-kv260_DPUCZDX8G_ISA1_B3136-r2.5.0.tar.gz
   md5: 506c438812fee2c092ee302d1ff2abf5
```

Both checksums were verified against the published values during the fetch
on 2026-04-30. The tarballs themselves are not retained in this folder
(the extracted contents are sufficient for deployment); they can be
re-downloaded at any time from the URLs above.

## How to rebuild from Caffe source

The `.xmodel` files above are pre-compiled for the production DPU. To
reproduce the compilation from the Caffe source, follow the procedure in
Appendix A of the report (`master-draft/build/chapters/11_appendix_caffe.md`).
The summary is:

```sh
# 1. Start the Vitis AI 2.5 Docker image with GPU support
docker run --gpus all -v $(pwd):/workspace -it xilinx/vitis-ai-cpu:2.5.0
conda activate vitis-ai-caffe

# 2. Download Caffe source through the Model Zoo downloader.py
#    (the public openDownload URL for the .zip Caffe sources is gated;
#     the official path is the downloader.py script in the Vitis AI repo)
cd /workspace/Vitis-AI/model_zoo
python downloader.py --model cf_refinedet_coco_480_360_0.96_5.08G_2.5
python downloader.py --model cf_densebox_wider_640_360_1.11G_2.5

# 3. Quantise to INT8 with a calibration set of ~1000 frames
vai_q_caffe quantize -model float.prototxt -weights float.caffemodel \
    -calib_iter 100 -test_iter 50 -output_dir quantize_results/

# 4. Compile against the DPU arch.json shipped here
vai_c_caffe -p quantize_results/deploy.prototxt \
    -c quantize_results/deploy.caffemodel \
    -a /workspace/2_Implementation/Source_Code/ML_Models_Vitis_AI/arch_dpu_kv260.json \
    -o compile_results/ -n refinedet_kv260
```

The Caffe `.prototxt` and `.caffemodel` files for the FP32 source are not
included here because they are gated behind the Vitis AI Model Zoo
downloader and are large (each model is 50 to 100 MB of weights). The
deployable `.xmodel` files at one tenth the size are sufficient for
running the production system, and the rebuild path above is documented
for the case where a developer needs to re-quantise the model from a new
calibration set.

## Deploy to the board

```sh
scp -r xmodel_deployable/refinedet_pruned_0_96 \
       petalinux@192.168.1.78:/usr/share/vitis_ai_library/models/

scp -r xmodel_deployable/densebox_640_360 \
       petalinux@192.168.1.78:/usr/share/vitis_ai_library/models/

ssh petalinux@192.168.1.78 'systemctl restart kv260-multichannel-openamp.service'
```

The `vvas_xinfer` element loads the models from
`/usr/share/vitis_ai_library/models/<name>/<name>.xmodel` at pipeline
startup. The kernel JSONs in `../JSON_Configurations/` reference these
paths through their `infer_json` field.
