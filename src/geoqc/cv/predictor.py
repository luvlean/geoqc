"""Predictor interface and backends.

``Predictor.predict(tile: np.ndarray) -> list[Mask]`` where ``tile`` is an RGB uint8 array
(H, W, 3). Backends:

* :class:`UltralyticsSegPredictor` - YOLOv8-seg instance segmentation (weights from
  ``geoqc cv fetch-weights`` or fine-tuned in ``notebooks/train_finetune.ipynb``).
* :class:`MockPredictor` - brightness threshold + connected components; no weights, no
  network, deterministic. Used by tests and for smoke runs (``backend: mock``).

Device selection: ``auto`` -> cuda > mps > cpu. ``half`` is enabled on CUDA only.
"""

from __future__ import annotations

import os
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from geoqc.config import resolve_path

# 8-connectivity structuring element for connected components.
_STRUCT8 = np.ones((3, 3), dtype=bool)


@dataclass
class Mask:
    """One predicted instance: boolean mask at tile resolution and a confidence in [0, 1]."""

    array: np.ndarray  # H x W, bool
    score: float
    label: str = "building"

    @property
    def area_px(self) -> int:
        return int(self.array.sum())


class Predictor(ABC):
    """Abstract building segmentation model."""

    name: str = "predictor"
    device: str = "cpu"

    @abstractmethod
    def predict(self, tile: np.ndarray) -> list[Mask]:
        """Return instance masks for one RGB uint8 tile (H, W, 3)."""

    def predict_batch(self, tiles: list[np.ndarray]) -> list[list[Mask]]:
        """Default batching: sequential. Backends override with real batched inference."""
        return [self.predict(t) for t in tiles]


def resolve_device(device: str | None = "auto") -> str:
    """Map ``auto|cuda|mps|cpu`` to an available torch device string."""
    dev = (device or "auto").lower()
    if dev == "cpu":
        return "cpu"
    import torch

    if dev == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    if dev.startswith("cuda") and not torch.cuda.is_available():
        return "cpu"
    if dev == "mps" and not torch.backends.mps.is_available():
        return "cpu"
    return dev


def seed_everything(seed: int = 42) -> None:
    """Fix Python / NumPy / torch seeds so that predict is reproducible."""
    random.seed(seed)
    np.random.seed(seed)
    os.environ.setdefault("PYTHONHASHSEED", str(seed))
    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:  # pragma: no cover
        pass


def _check_tile(tile: np.ndarray) -> np.ndarray:
    if tile.ndim != 3 or tile.shape[2] != 3:
        raise ValueError(f"tile must be H x W x 3 RGB, got shape {tile.shape}")
    if tile.dtype != np.uint8:
        tile = np.clip(tile, 0, 255).astype(np.uint8)
    return tile


def components_to_masks(
    binary: np.ndarray,
    prob: np.ndarray | None = None,
    *,
    min_component_px: int = 1,
    default_score: float = 1.0,
) -> list[Mask]:
    """Split a binary mask into 8-connected components; score = mean ``prob`` inside each."""
    labels, n = ndimage.label(binary, structure=_STRUCT8)
    if n == 0:
        return []
    masks: list[Mask] = []
    sizes = ndimage.sum(np.ones_like(binary, dtype=np.int32), labels, index=range(1, n + 1))
    means = (
        ndimage.mean(prob, labels, index=range(1, n + 1))
        if prob is not None
        else [default_score] * n
    )
    for i, (size, mean) in enumerate(zip(sizes, means, strict=True), start=1):
        if size < min_component_px:
            continue
        masks.append(Mask(array=labels == i, score=float(np.clip(mean, 0.0, 1.0))))
    # Deterministic order: largest first, then by first pixel index.
    masks.sort(key=lambda m: (-m.area_px, int(np.argmax(m.array))))
    return masks


class MockPredictor(Predictor):
    """Bright-blob detector for tests: pixels with all channels >= threshold form instances.

    Score = base_score + (1 - base_score) * mean_intensity / 255, so brighter blobs get a
    higher confidence; this makes threshold sweeps meaningful on synthetic tiles.
    """

    name = "mock"

    def __init__(
        self, threshold: int = 200, min_component_px: int = 4, base_score: float = 0.5
    ) -> None:
        self.threshold = int(threshold)
        self.min_component_px = int(min_component_px)
        self.base_score = float(base_score)
        self.device = "cpu"

    def predict(self, tile: np.ndarray) -> list[Mask]:
        tile = _check_tile(tile)
        bright = (tile >= self.threshold).all(axis=2)
        intensity = tile.mean(axis=2) / 255.0
        prob = self.base_score + (1.0 - self.base_score) * intensity
        return components_to_masks(bright, prob, min_component_px=self.min_component_px)


class UltralyticsSegPredictor(Predictor):
    """YOLOv8-seg instance segmentation via the ``ultralytics`` package."""

    name = "ultralytics"

    def __init__(
        self,
        weights: str | Path,
        *,
        conf: float = 0.25,
        nms_iou: float = 0.5,
        imgsz: int = 512,
        device: str = "auto",
        half_on_cuda: bool = True,
        max_det: int = 300,
        mask_threshold: float = 0.5,
        seed: int = 42,
    ) -> None:
        self.weights = Path(resolve_path(weights))
        if not self.weights.exists():
            raise FileNotFoundError(
                f"weights not found: {self.weights} (run `geoqc cv fetch-weights`)"
            )
        self.conf = float(conf)
        self.nms_iou = float(nms_iou)
        self.imgsz = int(imgsz)
        self.device = resolve_device(device)
        self.half = bool(half_on_cuda) and self.device.startswith("cuda")
        self.max_det = int(max_det)
        self.mask_threshold = float(mask_threshold)
        seed_everything(seed)
        os.environ.setdefault("YOLO_VERBOSE", "False")
        from ultralytics import YOLO

        self.model = YOLO(str(self.weights))
        self.class_names: dict[int, str] = dict(getattr(self.model, "names", {}) or {})

    def _results_to_masks(self, result: Any, shape: tuple[int, int]) -> list[Mask]:
        if result.masks is None or result.boxes is None or len(result.boxes) == 0:
            return []
        data = result.masks.data.detach().float().cpu().numpy()  # (N, h, w)
        scores = result.boxes.conf.detach().float().cpu().numpy()
        classes = result.boxes.cls.detach().int().cpu().numpy()
        masks: list[Mask] = []
        for arr, score, cls in zip(data, scores, classes, strict=True):
            if arr.shape != shape:
                import cv2

                arr = cv2.resize(arr, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
            binary = arr >= self.mask_threshold
            if not binary.any():
                continue
            label = self.class_names.get(int(cls), str(int(cls)))
            masks.append(Mask(array=binary, score=float(score), label=str(label).lower()))
        return masks

    def predict_batch(self, tiles: list[np.ndarray]) -> list[list[Mask]]:
        if not tiles:
            return []
        tiles = [_check_tile(t) for t in tiles]
        # Ultralytics treats numpy input as BGR (OpenCV convention) and flips it to RGB
        # internally, so RGB tiles are passed reversed.
        bgr = [np.ascontiguousarray(t[:, :, ::-1]) for t in tiles]
        results = self.model.predict(
            source=bgr,
            conf=self.conf,
            iou=self.nms_iou,
            imgsz=self.imgsz,
            device=self.device,
            half=self.half,
            max_det=self.max_det,
            retina_masks=True,
            verbose=False,
        )
        return [
            self._results_to_masks(r, (t.shape[0], t.shape[1]))
            for r, t in zip(results, tiles, strict=True)
        ]

    def predict(self, tile: np.ndarray) -> list[Mask]:
        return self.predict_batch([tile])[0]


def build_predictor(
    cv_cfg: dict[str, Any],
    *,
    device: str | None = None,
    conf: float | None = None,
    backend: str | None = None,
) -> Predictor:
    """Instantiate the backend named in ``config/cv.yaml`` (``ultralytics|mock``)."""
    backend = (backend or cv_cfg.get("backend") or "ultralytics").lower()
    device = device or cv_cfg.get("device", "auto")
    conf = float(conf if conf is not None else cv_cfg.get("conf_threshold", 0.25))
    seed = int(cv_cfg.get("seed", 42))
    if backend == "mock":
        mock = cv_cfg.get("mock", {}) or {}
        return MockPredictor(
            threshold=int(mock.get("threshold", 200)),
            min_component_px=int(mock.get("min_component_px", 4)),
            base_score=float(mock.get("base_score", 0.5)),
        )
    if backend == "ultralytics":
        weights = (cv_cfg.get("weights") or {}).get("path", "models/yolov8m-building-seg.pt")
        return UltralyticsSegPredictor(
            weights,
            conf=conf,
            nms_iou=float(cv_cfg.get("nms_iou", 0.5)),
            imgsz=int(cv_cfg.get("imgsz", 512)),
            device=device,
            half_on_cuda=bool(cv_cfg.get("half_on_cuda", True)),
            max_det=int(cv_cfg.get("max_det", 300)),
            mask_threshold=float(cv_cfg.get("mask_threshold", 0.5)),
            seed=seed,
        )
    raise ValueError(f"unknown cv backend {backend!r}; expected ultralytics|mock")
