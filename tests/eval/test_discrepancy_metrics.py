"""Выборка на верификацию и precision по вердиктам студента."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from geoqc.eval.common import EvalInputError
from geoqc.eval.discrepancy_metrics import (
    SAMPLE_COLUMNS,
    counts_by_class,
    fetch_discrepancies_geojson,
    read_verification_csv,
    stratified_sample,
    verification_metrics,
    write_sample_csv,
)


def _pool(n_map=100, n_geom=60, n_img=40, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_map + n_geom + n_img
    return pd.DataFrame(
        {
            "id": np.arange(1, n + 1),
            "class": ["missing_in_map"] * n_map
            + ["geometry_mismatch"] * n_geom
            + ["missing_on_image"] * n_img,
            "confidence": rng.uniform(0, 1, n),
            "iou": rng.uniform(0, 0.5, n),
            "lon": 37.0,
            "lat": 55.0,
        }
    )


def test_stratified_sample_size_quotas_and_queue_head():
    """Выборка >= 50, >= 15 на класс, содержит топ-20 по confidence, детерминирована по seed."""
    df = _pool()
    sample, meta = stratified_sample(df, sample_size=50, per_class_min=15, top_k_global=20, seed=42)
    assert len(sample) >= 50 and meta["n_selected"] == len(sample)
    assert all(v >= 15 for v in counts_by_class(sample).values())
    top20 = set(df.sort_values("confidence", ascending=False).head(20)["id"])
    assert top20 <= set(sample["id"])
    assert sample["id"].is_unique
    again, _ = stratified_sample(df, sample_size=50, per_class_min=15, top_k_global=20, seed=42)
    assert again["id"].tolist() == sample["id"].tolist()
    assert meta["selected_by_reason"]["top_global"] == 20
    assert meta["short_classes"] == []


def test_stratified_sample_small_class_taken_entirely():
    """Класс с 5 объектами попадает целиком, недобор отмечен в meta.short_classes."""
    df = _pool(n_map=80, n_geom=40, n_img=5)
    sample, meta = stratified_sample(df, sample_size=50, per_class_min=15, top_k_global=20, seed=0)
    assert counts_by_class(sample)["missing_on_image"] == 5
    assert meta["short_classes"] == ["missing_on_image"]
    assert len(sample) >= 50
    with pytest.raises(EvalInputError):
        stratified_sample(df.iloc[0:0])


def test_write_sample_csv_columns(tmp_path):
    """CSV выборки имеет ровно столбцы контракта, verdict и comment пустые."""
    sample, _ = stratified_sample(
        _pool(), sample_size=50, per_class_min=15, top_k_global=20, seed=1
    )
    path = tmp_path / "verification_sample.csv"
    write_sample_csv(sample, path)
    df = pd.read_csv(path)
    assert list(df.columns) == list(SAMPLE_COLUMNS)
    assert df["verdict"].isna().all() and df["comment"].isna().all()
    assert len(df) == len(sample)


def _verification_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": range(1, 10),
            "class": ["missing_in_map"] * 4 + ["geometry_mismatch"] * 2 + ["missing_on_image"] * 3,
            "confidence": [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55],
            "verdict": [
                "map_error",
                "map_error",
                "model_error",
                "unsure",
                "map_error",
                "image_issue",
                "map_error",
                "",
                "bogus",
            ],
        }
    )


def test_verification_metrics_per_class_overall_and_precision_at_n():
    """precision = map_error/(n − unsure) по классам и в целом; Precision@N по топ-N среди проверенных."""
    res = verification_metrics(_verification_frame(), [3, 5, 10])
    assert res["n_rows"] == 9 and res["n_checked"] == 7 and res["n_unchecked"] == 2
    assert res["invalid_verdicts"] == ["bogus"]
    by = res["verified_by_class"]
    assert by["missing_in_map"] == {
        "n": 4,
        "unsure": 1,
        "true_error": 2,
        "model_error": 1,
        "image_issue": 0,
        "precision": pytest.approx(2 / 3),
    }
    assert by["geometry_mismatch"]["precision"] == pytest.approx(0.5)
    assert by["missing_on_image"]["precision"] == pytest.approx(1.0)
    assert res["precision_overall"] == pytest.approx(4 / 6)
    at3, at5, at10 = (
        res["precision_at_n"]["3"],
        res["precision_at_n"]["5"],
        res["precision_at_n"]["10"],
    )
    assert at3["value"] == pytest.approx(2 / 3) and at3["complete"] is True
    assert at5["value"] == pytest.approx(3 / 4) and at5["value_incl_unsure"] == pytest.approx(3 / 5)
    assert (
        at10["complete"] is False
        and at10["n_available"] == 7
        and at10["value"] == pytest.approx(4 / 6)
    )


def test_verification_metrics_no_verdicts_gives_none():
    """Ни одного вердикта -> precision None (не 0), а не ошибка."""
    df = _verification_frame()
    df["verdict"] = ""
    res = verification_metrics(df, [20])
    assert res["n_checked"] == 0 and res["precision_overall"] is None
    assert res["precision_at_n"]["20"]["value"] is None
    assert all(b["precision"] is None for b in res["verified_by_class"].values())


def test_read_verification_csv_from_synthetic_file(tmp_path):
    """Чтение заполненного CSV нормализует вердикты (пробелы, регистр) и требует обязательные столбцы."""
    path = tmp_path / "verification.csv"
    df = _verification_frame()
    df.loc[0, "verdict"] = " Map_Error "
    df.to_csv(path, index=False)
    loaded = read_verification_csv(path)
    assert loaded.loc[0, "verdict"] == "map_error"
    assert pd.isna(loaded.loc[7, "verdict"])
    pd.DataFrame({"id": [1]}).to_csv(tmp_path / "bad.csv", index=False)
    with pytest.raises(EvalInputError, match="lacks columns"):
        read_verification_csv(tmp_path / "bad.csv")
    with pytest.raises(EvalInputError, match="not found"):
        read_verification_csv(tmp_path / "missing.csv")


def test_fetch_discrepancies_geojson(synthetic_env):
    """Fallback-источник: экспорт §3.8 читается в таблицу с lon/lat и сортируется по confidence DESC."""
    df = fetch_discrepancies_geojson(synthetic_env.discrepancies_geojson)
    assert len(df) == 90 and df["confidence"].is_monotonic_decreasing
    assert counts_by_class(df) == {
        "missing_in_map": 45,
        "missing_on_image": 15,
        "geometry_mismatch": 30,
    }
    assert df["lon"].between(30, 45).all() and df["lat"].between(50, 60).all()


@pytest.mark.db
def test_fetch_discrepancies_db(db_conn):
    """Чтение из таблицы discrepancies тестовой БД: id, class, confidence, точка внутри геометрии."""
    from geoqc.eval.discrepancy_metrics import fetch_discrepancies_db

    with db_conn.cursor() as cur:
        for cls, conf, wkt in (
            ("missing_in_map", 0.9, "POLYGON((37 55, 37.001 55, 37.001 55.001, 37 55.001, 37 55))"),
            (
                "geometry_mismatch",
                0.4,
                "POLYGON((37.01 55, 37.011 55, 37.011 55.001, 37.01 55.001, 37.01 55))",
            ),
            (
                "missing_on_image",
                0.7,
                "POLYGON((37.02 55, 37.021 55, 37.021 55.001, 37.02 55.001, 37.02 55))",
            ),
        ):
            cur.execute(
                "INSERT INTO discrepancies (class, iou, confidence, geom, model_version) "
                "VALUES (%s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4326)), %s)",
                (cls, 0.2, conf, wkt, "test-v"),
            )
    df = fetch_discrepancies_db(db_conn)
    assert df["class"].tolist() == ["missing_in_map", "missing_on_image", "geometry_mismatch"]
    assert 37.0 < df.loc[0, "lon"] < 37.001 and 55.0 < df.loc[0, "lat"] < 55.001
    assert fetch_discrepancies_db(db_conn, model_version="other").empty
