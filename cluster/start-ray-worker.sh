#!/usr/bin/env bash
# Join this node to the Ray cluster as a WORKER, inside the vLLM container.
#
# All addressing comes from bin/arena.conf (which auto-detects this node's
# fast-link IP from $MN_IF_NAME and derives the head's from CLUSTER_SUBNET).
# Override via env or bin/arena.conf.local -- do NOT hardcode addresses here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPARK_ROLE=worker
# shellcheck source=../bin/arena.conf
. "$HERE/../bin/arena.conf"

if [ -z "${VLLM_HOST_IP:-}" ]; then
  err "Could not determine this node's address on the fast link."
  err "  interface tried: $MN_IF_NAME"
  err "  fix: bring the link up, or set VLLM_HOST_IP / MN_IF_NAME in bin/arena.conf.local"
  exit 1
fi

if [ "$VLLM_HOST_IP" = "$HEAD_NODE_IP" ]; then
  err "This node's fast-link IP ($VLLM_HOST_IP) equals the head's ($HEAD_NODE_IP)."
  err "Either this script is running on the head, or the link is misconfigured."
  exit 1
fi

c "Ray WORKER $VLLM_HOST_IP -> head $HEAD_NODE_IP (iface $MN_IF_NAME)"

cd ~
bash ~/run_cluster.sh "$VLLM_IMAGE" "$HEAD_NODE_IP" --worker "$HF_CACHE" \
  ${PARSER_FILE:+-v "$PARSER_FILE:/app/super_v3_reasoning_parser.py:ro"} \
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
