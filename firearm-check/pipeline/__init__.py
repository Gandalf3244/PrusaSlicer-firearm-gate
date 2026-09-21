"""Printed-gun part identification: the runtime half of the detection pipeline.

  check       -> one call: model in, ALLOW / BLOCK out (what PrusaSlicer runs)
  units       -> load a mesh, infer / apply units, repair winding
  primitives  -> plane / cylinder / cone fitting on mesh faces
  signature   -> per-part primitive-composition signature
  fingerprint -> compound dimension sets and their tolerance matching
  detect      -> match a signature against data/fingerprints.json
  families    -> platform-level evidence from data/family_patterns.json
  calibers    -> bore evidence (a long hole at a bullet diameter)
  roles, tolerances, pool -> part roles, tolerance bands, memory trimming

The library-building half (inventory, batch signature fitting, fingerprint
mining, curation, family-pattern mining) lives in the development project
("Gun 3d model detection" on Google Drive) and produces the two JSON files
in data/.
"""
