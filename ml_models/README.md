# ML Models

INT8 DPU-deployable models from the Vitis AI 3.0 Model Zoo, plus the DPU
configuration metadata required at runtime.

* `xmodel_deployable/refinedet_pruned_0_96/` - person detector. Source:
  `cf_refinedet_coco_360_480_25G_2.5`.
* `xmodel_deployable/refinedet_pruned_0_96_acc/` - accuracy-tuned variant of
  the same RefineDet model.
* `xmodel_deployable/densebox_640_360/` - face detector. Source:
  `cf_densebox_640_360_1.11G_2.5`.
* `arch_dpu_kv260.json` - DPU architecture descriptor.
* `image_processing.cfg` - image processing kernel runtime config.
* `shell.json` - DPU shell metadata.

Both networks run independently on the full frame at INT8. A cascade-on-ROI
configuration is left as future work.
