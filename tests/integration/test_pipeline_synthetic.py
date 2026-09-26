"""Сквозной синтетический прогон: mock-детекция → PostGIS → matching → eval detection → gates."""

from __future__ import annotations

import pytest
from shapely.geometry import MultiPolygon, box

from geoqc.config import load_config
from geoqc.cv.pipeline import load_to_db, run_predict, write_detections
from geoqc.cv.predictor import MockPredictor
from geoqc.eval.common import EvalPaths, write_json
from geoqc.eval.detection_metrics import evaluate_split
from geoqc.eval.gates import METRICS_TEMPLATE, build_metrics
from geoqc.matching.matcher import run_matching
from tests.cv.conftest import CRS_UTM, SQUARES
from tests.matching.conftest import to_wgs_wkt

pytestmark = pytest.mark.db
MV = "mock-00000000"
SRID = int(CRS_UTM.split(":")[1])


def _ref(conn, rect, osm_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO buildings_ref (osm_id, geom, source) "
            "VALUES (%s, ST_GeomFromText(%s, 4326), 'osm')",
            (osm_id, to_wgs_wkt(MultiPolygon([box(*rect)]))),
        )


def _keys(d, prefix=""):
    out = set()
    for k, v in d.items():
        out.add(prefix + k)
        if isinstance(v, dict):
            out |= _keys(v, f"{prefix}{k}.")
    return out


def test_synthetic_end_to_end(synthetic_aoi, db_conn, tmp_path):
    """Известные объекты дают ожидаемые классы расхождений, а metrics.json — схему §3.9."""
    # 1. detection on the synthetic tiles: A, B, C found, the speck filtered out
    res = run_predict(
        synthetic_aoi["cv"],
        synthetic_aoi["pipeline"],
        split="all",
        predictor=MockPredictor(min_component_px=1),
    )
    assert len(res.detections) == 3
    det_path = synthetic_aoi["data"] / "interim" / "detections.geojson"
    write_detections(res.detections, det_path)

    # 2. map: A exact, B shifted by 8 m (IoU ~0.43), C absent, D exists only in the map
    ax0, ay0, ax1, ay1, _ = SQUARES["A"]
    bx0, by0, bx1, by1, _ = SQUARES["B"]
    _ref(db_conn, (ax0, ay0, ax1, ay1), 1)
    _ref(db_conn, (bx0 + 8, by0, bx1 + 8, by1), 2)
    _ref(db_conn, (500400.0, 5999700.0, 500420.0, 5999720.0), 4)
    assert load_to_db(res.detections, MV, db_conn) == 3

    # 3. matching without the real AOI of the repository
    cfg = load_config()
    cfg["matching"]["aoi_path"] = None
    run = run_matching(db_conn, MV, cfg=cfg, metric_srid=SRID)
    db_conn.commit()
    assert run.n_matches == 1
    assert run.counts_by_class == {
        "missing_in_map": 1,
        "missing_on_image": 1,
        "geometry_mismatch": 1,
    }

    # 4. detection metrics on the synthetic val split (labels = A, B, C, verified)
    reports = tmp_path / "reports"
    paths = EvalPaths.from_config(
        cfg,
        reports_dir=reports,
        labels_dir=synthetic_aoi["data"] / "labels",
        tiles_index=synthetic_aoi["data"] / "tiles" / "tiles.gpkg",
        tiles_dir=synthetic_aoi["data"] / "tiles",
        detections=det_path,
        manifest=tmp_path / "no_manifest.json",
        aoi=tmp_path / "no_aoi.geojson",
    )
    ev = evaluate_split("val", paths, cfg)
    assert ev.labels_verified
    report = ev.to_report("2026-01-01T00:00:00Z", None, smoke=False)
    d = report["detection"]
    assert (d["tp"], d["fp"], d["fn"]) == (3, 0, 0)
    assert d["f1"] == pytest.approx(1.0)
    assert d["mean_iou_matched"] > 0.9
    write_json(paths.metrics("val"), report)

    # 5. gates: fixed schema, val block filled, missing inputs reported, not invented
    metrics = build_metrics(paths, cfg, generated_at="2026-01-01T00:00:00Z", git_commit="x")
    assert _keys(METRICS_TEMPLATE) <= _keys(metrics)
    assert metrics["detection"]["val"]["tp"] == 3
    assert metrics["detection"]["test"]["f1"] is None
    assert set(metrics["gates"]) == {f"G{i}" for i in range(1, 10)}
    assert metrics["gates"]["G3"]["passed"] is True
    assert metrics["gates"]["G4"]["passed"] is not True
