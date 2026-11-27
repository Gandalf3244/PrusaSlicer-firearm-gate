"""Learned "is this a gun part" score: second signal for the AI-gated rules in check.py.

A mesh in mm is drawn from 12 directions in its own principal frame (render_views: point
splatting in numpy, no OpenGL, the same code made the training images); each view goes
through a frozen DINOv2-Base image encoder (ONNX, onnxruntime on the CPU) and a small
trained head pools the 12 views (attention + max) with the part's size. The head is 25
copies - 5 cross-validation folds x 5 seeds, every library design held out of one fold -
and the score is their mean, the exact quantity the threshold was calibrated on
(~/gun3d-data/ai: heads.py, fresh_eval.py; data/ai/ai.json).

The model files live in data/ai/ (dinov2_base.onnx, heads.npz, ai.json). Without them, or
without onnxruntime, available() is False and the checker runs as before. DINOv2 is Apache
2.0 (Meta Platforms); data/ai/LICENSE-DINOv2.txt.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parents[1] / "data" / "ai"
S = 224                 # image size
N_POINTS = 300_000      # surface samples per mesh
XMAG = 1.05             # orthographic half-width, in bounding radii
MEAN = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
STD = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)


def view_dirs() -> np.ndarray:
    """6 axis views + 6 corner views in the principal frame (x = longest axis)."""
    d = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1],
                  [1, 1, 1], [-1, 1, 1], [1, -1, 1], [-1, -1, 1], [1, 1, -1], [-1, -1, -1]], float)
    return d / np.linalg.norm(d, axis=1, keepdims=True)


def principal_frame(p: np.ndarray) -> np.ndarray:
    """Rows = the principal axes of points `p` (centred), largest first, each signed so the
    third moment along it is positive; right-handed. Independent of the file's orientation."""
    w, v = np.linalg.eigh(np.cov(p.T))
    v = v[:, ::-1].T.copy()
    for k in range(3):
        if ((p @ v[k]) ** 3).mean() < 0:
            v[k] = -v[k]
    if np.linalg.det(v) < 0:
        v[2] = -v[2]
    return v


def _camera(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Image x / y axes for a view along -d (d points at the camera)."""
    up = np.array([0, 0, 1.0]) if abs(d[2]) < 0.9 else np.array([0, 1.0, 0])
    x = np.cross(up, d)
    x /= np.linalg.norm(x)
    return x, np.cross(d, x)


def render_views(mesh, n_points: int = N_POINTS) -> tuple[np.ndarray, float] | None:
    """(12, S, S) uint8 grey shaded views of `mesh` (mm) and log(bounding radius / mm),
    or None for a mesh without area. Deterministic: same mesh -> same images."""
    import trimesh
    from scipy import ndimage
    if len(mesh.faces) == 0 or not mesh.area > 0:
        return None
    pts, fi = trimesh.sample.sample_surface(mesh, n_points, seed=np.random.default_rng(0))
    nrm = mesh.face_normals[fi]
    c = (pts.max(0) + pts.min(0)) / 2
    pts = pts - c
    rad = float(np.linalg.norm(pts, axis=1).max())
    if not rad > 0:
        return None
    R = principal_frame(pts[::max(1, n_points // 4096)])
    pts = (pts @ R.T) / rad
    nrm = nrm @ R.T
    out = np.zeros((12, S, S), np.uint8)
    for k, d in enumerate(view_dirs()):
        ax, ay = _camera(d)
        u = np.floor(((pts @ ax) / XMAG + 1) * 0.5 * S).astype(np.int64)
        v = np.floor((1 - (pts @ ay) / XMAG) * 0.5 * S).astype(np.int64)
        ok = (u >= 0) & (u < S) & (v >= 0) & (v < S)
        pix = (v * S + u)[ok]
        depth = (pts @ d)[ok]
        shade = 0.25 + 0.75 * np.abs(nrm[ok] @ d)        # ambient + two-sided light from the camera
        order = np.lexsort((-depth, pix))                    # per pixel, nearest first
        pix, shade, depth = pix[order], shade[order], depth[order]
        first = np.ones(len(pix), bool)
        first[1:] = pix[1:] != pix[:-1]
        img = np.zeros(S * S, np.float32)
        dep = np.full(S * S, -np.inf, np.float32)
        img[pix[first]] = shade[first]
        dep[pix[first]] = depth[first]
        img, dep = img.reshape(S, S), dep.reshape(S, S)
        # a far surface seen through a gap between the near surface's samples: much deeper
        # than a neighbour -> a gap, refilled from the near neighbours below
        far = dep < ndimage.maximum_filter(dep, size=3) - 4 * XMAG / S
        img[far] = 0
        for _ in range(2):
            have = (img > 0).astype(np.float32)
            den = ndimage.uniform_filter(have, 3)
            num = ndimage.uniform_filter(img, 3)
            gap = (img == 0) & (den >= 4 / 9)
            img[gap] = num[gap] / den[gap]
        out[k] = np.clip(img * 0.7 * 255, 0, 255).astype(np.uint8)
    return out, math.log(rad)


def images_to_input(views: np.ndarray) -> np.ndarray:
    """(V, S, S) uint8 -> (V, 3, S, S) float32 normalized for the encoder."""
    x = views.astype(np.float32)[:, None] / 255.0
    return (np.repeat(x, 3, axis=1) - MEAN) / STD


def _gelu(x):
    from scipy.special import erf
    return 0.5 * x * (1.0 + erf(x / math.sqrt(2.0)))


def head_scores(E: np.ndarray, logsize: float, H: dict) -> np.ndarray:
    """E (V, D) view embeddings -> sigmoid score of every stored head (folds x seeds)."""
    out = []
    for f in range(H["mu"].shape[0]):
        x = (E - H["mu"][f]) / H["sd"][f]
        ls = (logsize - H["ls_mu"][f]) / H["ls_sd"][f]
        for s in range(H["w1"].shape[1]):
            h = _gelu(x @ H["w1"][f, s].T + H["b1"][f, s])              # (V, h)
            a = h @ H["wa"][f, s][0] + H["ba"][f, s][0]
            a = np.exp(a - a.max())
            a /= a.sum()
            z = np.concatenate([(a[:, None] * h).sum(0), h.max(0), [ls]])
            logit = float(z @ H["wo"][f, s][0] + H["bo"][f, s][0])
            out.append(1.0 / (1.0 + math.exp(-logit)))
    return np.array(out)


class Model:
    """Lazily loaded encoder + heads; one per process."""

    def __init__(self, path: Path = DATA):
        self.path = path
        self.cfg = json.loads((path / "ai.json").read_text())
        self._sess = None
        self._heads = None

    def _load(self):
        if self._sess is None:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.log_severity_level = 3
            self._sess = ort.InferenceSession(str(self.path / self.cfg["encoder"]), so,
                                              providers=["CPUExecutionProvider"])
            z = np.load(self.path / self.cfg["heads"])
            self._heads = {k: z[k] for k in z.files}

    def embed(self, views: np.ndarray) -> np.ndarray:
        self._load()
        x = images_to_input(views)
        name = self._sess.get_inputs()[0].name
        return np.concatenate([self._sess.run(None, {name: x[i:i + 4]})[0] for i in range(0, len(x), 4)])

    def score(self, mesh) -> float | None:
        """Mean head score of a mesh in mm, or None when it cannot be drawn."""
        r = render_views(mesh)
        if r is None:
            return None
        views, logsize = r
        self._load()
        return float(head_scores(self.embed(views), logsize, self._heads).mean())

    @property
    def threshold(self) -> float:
        return float(self.cfg["threshold"])


_MODEL: Model | None = None
_TRIED = False


def model() -> Model | None:
    """The installed model, or None (files or onnxruntime missing): the AI rules are off."""
    global _MODEL, _TRIED
    if not _TRIED:
        _TRIED = True
        try:
            import onnxruntime  # noqa: F401
            if (DATA / "ai.json").exists():
                _MODEL = Model()
        except ImportError:
            _MODEL = None
    return _MODEL
