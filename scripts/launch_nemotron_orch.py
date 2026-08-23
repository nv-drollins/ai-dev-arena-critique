#!/usr/bin/env python3
"""Set MODEL_NAME and start orchestrator."""
import os
os.environ["MODEL_NAME"] = "nvidia/nemotron-3-super"
os.environ["VLLM_URL"] = "http://127.0.0.1:8000"
os.chdir("/home/nvidia/ai-dev-arena")

from uvicorn.main import main
main(["orchestrator.main:app", "--host", "0.0.0.0", "--port", "8080"])
