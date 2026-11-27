"""Smoke test of the bundled AI score (pipeline/ai.py) in the Windows checker runtime:
the model loads under the embeddable Python, and a plain box scores far below the threshold.
    ..\\python\\python.exe test_ai.py      (from checker-runtime\\app)
"""
import sys
import time

import trimesh

from pipeline import ai

m = ai.model()
if m is None:
    sys.exit("AI model not available (data/ai/ or onnxruntime missing)")
t = time.time()
s = m.score(trimesh.creation.box((30, 20, 10)))
print(f"AI score of a box: {s:.4f} (threshold {m.threshold}), {time.time() - t:.1f} s")
sys.exit(0 if s is not None and s < m.threshold else 1)
