#!/usr/bin/env bash
export MODEL_NAME=openai/gpt-oss-120b
export VLLM_URL=http://127.0.0.1:8000
cd /home/nvidia/ai-dev-arena
exec .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080
