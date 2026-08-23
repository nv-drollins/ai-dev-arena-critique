#!/usr/bin/env python3
"""Render /api/telemetry for the terminal status block."""
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("  telemetry: not available (%s)" % type(e).__name__)
    sys.exit(0)
for s in d.get("sparks", []):
    print("  %-12s  gpu=%3s%%  mem=%3s%%  %s" %
          (s.get("name","?"), s.get("gpu_util","-"), s.get("mem_util","-"),
           s.get("role","-").upper() if s.get("role") else ("OFFLINE" if not s.get("online") else "online")))
m = d.get("model") or {}
if m.get("display") or m.get("served"):
    print("  %-12s  %s" % ("model", m.get("display") or m.get("served")))
