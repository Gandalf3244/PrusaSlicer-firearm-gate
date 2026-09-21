"""3D part detection pipeline over the FOSSCAD reference library.

Modules follow the build order in part-detection-pipeline-report.md:
  inventory   -> catalog reference STLs with model/part labels (step 0)
  primitives  -> plane/cylinder fitting on mesh faces (step 1)
  signature   -> per-part primitive-composition signature (steps 1-2)
  similarity  -> pairwise signature comparison across the library (step 2)
"""
