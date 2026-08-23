"""Spark cluster telemetry — polls GPU/CPU/memory across all DGX Spark nodes.

Scalable: SPARK_NODES is a list, so adding a 3rd or 4th node is just adding
an entry. Each node reports:
  - gpu_util : nvidia-smi GPU utilization %
  - cpu_util : /proc/stat sampled over 400ms
  - mem_util : unified DRAM used % (GB10: GPU and CPU share the pool)
  - mem_used / mem_total : bytes

Node 0 ("localhost") runs the probe locally; remote nodes run the same
probe over SSH (BatchMode, so no password prompts hang the demo).
"""
import asyncio
import json
import os
import subprocess

# --- Node roster -------------------------------------------------------------
# Default: the two stacked Sparks. To scale to 3-4 nodes, set SPARK_NODES_JSON
# e.g.  export SPARK_NODES_JSON='{"sparks":[{"name":"spark-a","host":"localhost"},
#                                          {"name":"spark-b","host":"192.168.1.149"},
#                                          {"name":"spark-c","host":"192.168.1.150"}]}'
_DEFAULT_SPARKS = [
    {"name": "spark-6b64", "host": "localhost", "role": "head"},
    {"name": "spark-ce66", "host": "192.168.1.149", "role": "worker"},
]

def _env_sparks():
    raw = os.environ.get("SPARK_NODES_JSON")
    if not raw:
        return _DEFAULT_SPARKS
    try:
        d = json.loads(raw)
        return d if isinstance(d, list) else d.get("sparks", _DEFAULT_SPARKS)
    except Exception:
        return _DEFAULT_SPARKS

SPARKS = _env_sparks()

# Self-contained probe: prints one JSON line. Runs locally or over SSH.
_PROBE = r"""
import json, time, subprocess
def cpu_util():
    def read():
        with open('/proc/stat') as f:
            p = f.readline().split()[1:]
        a = [int(x) for x in p]
        idle = a[3] + (a[4] if len(a) > 4 else 0)
        return idle, sum(a)
    try:
        i1, t1 = read(); time.sleep(0.4); i2, t2 = read()
        tot = t2 - t1; idle = max(0, i2 - i1)
        return int(100 * (tot - idle) / tot) if tot > 0 else 0
    except Exception:
        return 0
mem = {}
for line in open('/proc/meminfo'):
    parts = line.split(':', 1)[1].split()
    if parts and parts[0].isdigit():
        mem[line.split(':', 1)[0]] = int(parts[0]) * 1024
total = mem.get('MemTotal', 0)
avail = mem.get('MemAvailable', mem.get('MemFree', 0))
out = {'cpu_util': cpu_util(), 'mem_total': total,
       'mem_used': max(0, total - avail),
       'mem_util': int(100 * (total - avail) / total) if total else 0,
       'gpu_util': 0}
# GPU compute utilization — on GB10 use `nvidia-smi dmon` 'sm' (compute
# engine %), which is the honest continuous load; single-shot
# utilization.gpu samples catch idle gaps between decode steps.
# Sample a 5s window and take the max so a bursty decode step is caught.
try:
    r = subprocess.run('nvidia-smi dmon -s u -c 5 --format=csv,noheader'.split(),
                       capture_output=True, text=True, timeout=14)
    sm_vals = []
    for row in r.stdout.strip().split('\n')[1:]:
        cols = [c.strip() for c in row.split(',')]
        if len(cols) >= 2 and cols[1].isdigit():
            sm_vals.append(int(cols[1]))
    if sm_vals:
        out['gpu_util'] = max(sm_vals)
    else:
        # fallback: single-shot utilization.gpu
        r2 = subprocess.run('nvidia-smi --query-gpu=utilization.gpu '
                            '--format=csv,noheader,nounits'.split(),
                            capture_output=True, text=True, timeout=5)
        first = r2.stdout.strip().split('\n')[0]
        out['gpu_util'] = int(first) if first.isdigit() else 0
except Exception:
    pass
print('TELEMETRY ' + json.dumps(out))
"""

def _run_probe_local(host: str) -> dict:
    r = subprocess.run(
        ["python3", "-c", _PROBE],
        capture_output=True, text=True, timeout=15,
    )
    for line in r.stdout.splitlines():
        if line.startswith("TELEMETRY "):
            return json.loads(line[len("TELEMETRY "):])
    raise RuntimeError(f"local probe on {host} failed: {r.stderr[:200]}")

def _run_probe_ssh(host: str, user: str = "nvidia") -> dict:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=5", f"{user}@{host}",
         "python3 -c " + _json_escape(_PROBE)],
        capture_output=True, text=True, timeout=20,
    )
    for line in r.stdout.splitlines():
        if line.startswith("TELEMETRY "):
            return json.loads(line[len("TELEMETRY "):])
    raise RuntimeError(f"ssh probe on {host} failed: {r.stderr[:200]}")

def _json_escape(code: str) -> str:
    return "'" + code.replace("'", "'\"'\"'") + "'"

async def collect_telemetry() -> dict:
    """Collect from all Sparks in parallel. Never raises — a dead node
    is reported as offline so the dashboard can still render."""
    loop = asyncio.get_running_loop()
    results = []

    async def one(spark: dict):
        entry = {
            "name": spark.get("name", spark["host"]),
            "host": spark["host"],
            "role": spark.get("role", ""),
            "online": False,
            "gpu_util": 0, "cpu_util": 0,
            "mem_util": 0, "mem_used": 0, "mem_total": 0,
        }
        try:
            fn = _run_probe_local if spark["host"] in ("localhost", "127.0.0.1") else _run_probe_ssh
            data = await loop.run_in_executor(None, fn, spark["host"])
            entry.update(data)
            entry["online"] = True
        except Exception as e:
            entry["error"] = str(e)[:120]
        return entry

    sparks = await asyncio.gather(*[one(s) for s in SPARKS])
    return {"sparks": list(sparks), "model": await _model_info(loop)}

async def _model_info(loop) -> dict:
    """Served model name + root (for the 'current model' badge)."""
    vllm = os.environ.get("VLLM_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        import urllib.request
        def fetch():
            with urllib.request.urlopen(vllm + "/v1/models", timeout=4) as r:
                return json.loads(r.read())
        d = await loop.run_in_executor(None, fetch)
        m = d["data"][0]
        return {"served": m["id"], "root": m.get("root", m["id"]),
                "max_model_len": m.get("max_model_len")}
    except Exception as e:
        return {"served": None, "root": None, "error": str(e)[:80]}
