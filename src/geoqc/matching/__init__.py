"""Matching of detected buildings against the OSM reference, discrepancy classification and scoring.

SQL side: ``sql/002_matching.sql`` (IoU, candidate pairs, greedy 1:1, classification, edge cases,
features). Python side: :mod:`geoqc.matching.matcher` (pipeline, Hungarian alternative),
:mod:`geoqc.matching.scoring` (confidence), :mod:`geoqc.matching.edge_cases`,
:mod:`geoqc.matching.export`, :mod:`geoqc.matching.cli`.
"""

from geoqc.matching.matcher import RunResult, run_matching

__all__ = ["RunResult", "run_matching"]
