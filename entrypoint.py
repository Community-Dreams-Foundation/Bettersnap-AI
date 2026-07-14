#!/usr/bin/env python3.11
"""Mode switch for the unified BetterSnap image (one image, two jobs).

MODE=train  -> run the per-user LoRA trainer  (/workspace/run_training.py)
otherwise   -> run inference / generation      (/app/main.py)

Each script assumes it runs from its own directory (main.py does `import catalog`
from /app; run_training.py writes under /workspace), so we chdir before exec. os.execvp
replaces this process, preserving the child's exit code for ACA.
"""
import os

mode = os.environ.get("MODE", "infer").strip().lower()
if mode.startswith("train"):
    os.chdir("/workspace")
    os.execvp("python3.11", ["python3.11", "run_training.py"])
else:
    os.chdir("/app")
    os.execvp("python3.11", ["python3.11", "main.py"])
