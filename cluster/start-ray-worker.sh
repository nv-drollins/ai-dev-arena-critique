#!/usr/bin/env bash
set -euo pipefail
export MN_IF_NAME=enp1s0f1np1
export VLLM_HOST_IP=192.168.100.11
export HEAD_NODE_IP=192.168.100.10
export VLLM_IMAGE=nvcr.io/nvidia/vllm:26.05-py3
cd ~
bash ~/run_cluster.sh "$VLLM_IMAGE" "$HEAD_NODE_IP" --worker ~/.cache/huggingface \
  -v "$HOME/nemotron-super/super_v3_reasoning_parser.py:/app/super_v3_reasoning_parser.py:ro" \
  -e VLLM_HOST_IP="$VLLM_HOST_IP" \
  -e UCX_NET_DEVICES="$MN_IF_NAME" \
  -e NCCL_SOCKET_IFNAME="$MN_IF_NAME" \
  -e OMPI_MCA_btl_tcp_if_include="$MN_IF_NAME" \
  -e GLOO_SOCKET_IFNAME="$MN_IF_NAME" \
  -e TP_SOCKET_IFNAME="$MN_IF_NAME" \
  -e RAY_memory_monitor_refresh_ms=0 \
  -e MASTER_ADDR="$HEAD_NODE_IP" \
  -e VLLM_NVFP4_GEMM_BACKEND=marlin \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  ${HF_TOKEN:+-e HF_TOKEN="$HF_TOKEN"} \
  -e VLLM_FLASHINFER_ALLREDUCE_BACKEND=trtllm \
  -e VLLM_USE_FLASHINFER_MOE_FP4=0
