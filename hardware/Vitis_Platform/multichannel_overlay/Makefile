# (C) Copyright 2020 - 2022 Xilinx, Inc.
# Copyright (C) 2022-2023 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Modified: Replaced pp_pipeline_accel (VVAS 2.x) with image_processing (VVAS 3.0)

ifndef DIR_PRJ
 DIR_PRJ = $(shell pwd)
endif

# Path to VVAS repo containing image_processing HLS source
# Default assumes: <workspace>/VVAS/  and  <workspace>/aibox-reid/aibox-reid/
ifndef HW_ACCEL_PATH
 HW_ACCEL_PATH = ../../VVAS
endif

# Path to DPU IP from Vitis-AI repo
# Default assumes: <workspace>/dpu_ip/  and  <workspace>/aibox-reid/aibox-reid/
ifndef DPU_TRD_PATH
 DPU_TRD_PATH = ../..
endif

RM = rm -f
RMDIR = rm -rf
VIVADO_ROOT := $(XILINX_VIVADO)
VIVADO:=${VIVADO_ROOT}/bin/vivado
TARGET := hw
DEVICE := mpsoc

# =====================================================================
# DPU kernel
# =====================================================================
DPU_KERN_NAME = DPUCZDX8G

dpu_HDLSRCS= ${DIR_PRJ}/kernel_xml/dpu/kernel.xml\
	     ${DIR_PRJ}/scripts/package_dpu_kernel.tcl\
	     ${DIR_PRJ}/scripts/gen_dpu_xo.tcl\
	     ${DIR_PRJ}/dpu_conf.vh\
	     ${DPU_TRD_PATH}/dpu_ip/Vitis/dpu/hdl/DPUCZDX8G.v\
	     ${DPU_TRD_PATH}/dpu_ip/Vitis/dpu/inc/arch_def.vh\
	     ${DPU_TRD_PATH}/dpu_ip/Vitis/dpu/xdc/*.xdc\
	     ${DPU_TRD_PATH}/dpu_ip/DPUCZDX8G_*/hdl/DPUCZDX8G_*_dpu.sv\
	     ${DPU_TRD_PATH}/dpu_ip/DPUCZDX8G_*/inc/function.vh\
	     ${DPU_TRD_PATH}/dpu_ip/DPUCZDX8G_*/inc/arch_para.vh

dpu_TCL=${DIR_PRJ}/scripts/gen_dpu_xo.tcl

# =====================================================================
# Image Processing kernel (VVAS 3.0 — replaces old pp_pipeline_accel)
# =====================================================================
IP_KERN_NAME = image_processing

image_processing_SRCS= ${HW_ACCEL_PATH}/image_processing/src/image_processing.cpp\
	     ${HW_ACCEL_PATH}/image_processing/src/v_hresampler.cpp\
	     ${HW_ACCEL_PATH}/image_processing/src/v_hscaler.cpp\
	     ${HW_ACCEL_PATH}/image_processing/src/v_dma.cpp\
	     ${HW_ACCEL_PATH}/image_processing/src/v_csc.cpp\
	     ${HW_ACCEL_PATH}/image_processing/src/v_vresampler.cpp\
	     ${HW_ACCEL_PATH}/image_processing/src/v_vscaler.cpp

IMAGE_PROCESSING_FLAGS := --kernel $(IP_KERN_NAME) -I. -I${HW_ACCEL_PATH}/image_processing/src/hls

# =====================================================================
# v++ linker config
# =====================================================================
XOCC_OPTS = -t ${TARGET} --platform ${PLATFORM} --save-temps \
	--config ${DIR_PRJ}/prj_conf/prj_config_1dpu \
	--xp param:compiler.userPostSysLinkOverlayTcl=${DIR_PRJ}/prj_conf/strip_interconnects.tcl

# Both kernel .xo files needed for linking
kernel_xo = binary_container_1/dpu.xo binary_container_1/image_processing.xo

JOBS = 32

# =====================================================================
# Build targets
# =====================================================================
.PHONY: all clean package help

help::
	@echo ""
	@echo "Makefile Usage:"
	@echo ""
	@echo "make all PLATFORM=<FPGA platform .xpfm>"
	@echo "   Builds the xclbin with DPU + image_processing kernel"
	@echo ""
	@echo "make clean"
	@echo "   Removes all generated files"
	@echo ""
	@echo "Required variables:"
	@echo "  PLATFORM        - Path to .xpfm file"
	@echo "  HW_ACCEL_PATH   - Path to VVAS repo (default: ../../VVAS)"
	@echo "  DPU_TRD_PATH    - Path to DPU IP repo (default: ../..)"
	@echo ""

all : binary_container_1/dpu.xclbin package

# --- DPU .xo generation (Vivado TCL flow, unchanged) ---
binary_container_1/dpu.xo: $(dpu_HDLSRCS)
	@mkdir -p $(@D)
	-@$(RM) $@
	$(VIVADO) -mode batch -source $(dpu_TCL) -notrace -tclargs $@ $(DPU_KERN_NAME) ${TARGET} ${DEVICE}

# --- Image Processing .xo generation (v++ HLS compile) ---
image_processing.cfg: image_processing_config.h
	./create_imgproc_config.sh

binary_container_1/image_processing.xo: $(image_processing_SRCS) image_processing.cfg
	@mkdir -p $(@D)
	v++ --platform $(PLATFORM) -t $(TARGET) -s $(IMAGE_PROCESSING_FLAGS) -c -o $@ $^

# --- Link both kernels into xclbin ---
binary_container_1/dpu.xclbin: $(kernel_xo)
	v++ $(XOCC_OPTS) -l --temp_dir binary_container_1 --log_dir binary_container_1/logs --remote_ip_cache binary_container_1/ip_cache -o "$@" $(+)

# --- Package outputs ---
package:
	-@mkdir -p binary_container_1/sd_card
	cp ./binary_*/link/vivado/vpl/prj/prj*/sources_1/bd/*/hw_handoff/*.hwh ./binary_*/sd_card
	cp ./binary_*/link/vivado/vpl/prj/prj.gen/sources_1/bd/*/ip/*_DPUCZDX8G_1_0/arch.json ./binary_*/sd_card
	cp ./binary_*/link/vivado/vpl/prj/prj.runs/impl_1/*.bit ./binary_*/sd_card
	cp ./binary_*/*.xclbin ./binary_*/sd_card
	cp image_processing.cfg ./binary_*/sd_card 2>/dev/null || true

clean:
	${RM} *.o *.elf *.log *.jou sample* v++* *.xclbin *.xclbin* *.cfg
	${RMDIR} binary_container_1/ packaged_*/ tmp_*/ .Xil/ _x/