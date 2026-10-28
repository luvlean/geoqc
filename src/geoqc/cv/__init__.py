"""Building detection: weights, predictors, vectorization, dataset export, sweep.

Heavy dependencies (torch, ultralytics) are imported lazily
inside the classes that need them so that ``geoqc --help`` stays fast.
"""

from geoqc.cv.predictor import Mask, Predictor
from geoqc.cv.vectorize import Detection

__all__ = ["Detection", "Mask", "Predictor"]
