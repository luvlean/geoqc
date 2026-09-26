"""Matching pipeline: edge cases -> 1:1 assignment -> classification -> scoring.

The greedy assignment and the classification live in SQL (``sql/002_matching.sql``); this module
orchestrates them, offers the Hungarian alternative (``scipy.optimize.linear_sum_assignment`` per
connected cluster of intersecting ref/det pairs) and writes ``discrepancies.confidence`` from
:mod:`geoqc.matching.scoring`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
from scipy.optimize import linear_sum_assignment

from geoqc.config import REPO_ROOT, load_config, resolve_path
from geoqc.matching import edge_cases
from geoqc.matching.scoring import Features, score_features

ALGORITHMS = ("greedy", "hungarian")
Pair = tuple[int, int, float]  # (ref_id, det_id, iou)


@dataclass
class RunResult:
    model_version: str
    metric_srid: int
    algorithm: str
    n_ref_osm: int = 0
    n_ref_secondary: int = 0
    n_det: int = 0
    n_det_split: int = 0
    n_det_suppressed: int = 0
    n_candidates: int = 0
    n_matches: int = 0
    mean_iou_matched: float | None = None
    counts_by_class: dict[str, int] = field(default_factory=dict)
    n_scored: int = 0
    n_aoi_edge: int = 0
    aoi_used: bool = False
    has_secondary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------------------------------------
# Resolution helpers
# ------------------------------------------------------------------------------------------------


def parse_srid(value: Any) -> int | None:
    """``"EPSG:32637"`` / ``32637`` / ``"32637"`` -> 32637; None / empty -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().upper()
    if s.startswith("EPSG:"):
        s = s[5:]
    return int(s)


def utm_epsg_for(lon: float, lat: float) -> int:
    """UTM zone EPSG for a lon/lat (falls back to a local implementation of the standard rule)."""
    try:  # Data Agent helper, docs/CONTRACTS.md §3.1
        from geoqc.data.crs import utm_epsg_for as _impl

        return int(_impl(lon, lat))
    except Exception:  # module missing or different signature
        zone = int(math.floor((lon + 180.0) / 6.0)) + 1
        zone = min(max(zone, 1), 60)
        return (32600 if lat >= 0 else 32700) + zone


def resolve_metric_srid(
    conn: psycopg.Connection, cfg: dict[str, Any], override: int | None = None
) -> int:
    """CLI option > ``pipeline.yaml crs.metric`` > ``data/manifest.json aoi.utm_epsg`` > data extent."""
    if override:
        return int(override)
    srid = parse_srid((cfg.get("crs") or {}).get("metric"))
    if srid:
        return srid
    manifest = REPO_ROOT / "data" / "manifest.json"
    if manifest.exists():
        try:
            srid = parse_srid(json.loads(manifest.read_text(encoding="utf-8"))["aoi"]["utm_epsg"])
            if srid:
                return srid
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            pass
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ST_X(c) AS lon, ST_Y(c) AS lat FROM ("
            "  SELECT ST_Centroid(ST_Extent(geom)) AS c FROM ("
            "    SELECT geom FROM buildings_ref WHERE source = 'osm'"
            "    UNION ALL SELECT geom FROM buildings_det) g) t"
        )
        row = cur.fetchone()
    if row and row["lon"] is not None:
        return utm_epsg_for(float(row["lon"]), float(row["lat"]))
    raise RuntimeError(
        "metric SRID unknown: set config/pipeline.yaml crs.metric or pass --metric-srid"
    )


def list_model_versions(conn: psycopg.Connection) -> list[tuple[str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT model_version, count(*) AS n FROM buildings_det GROUP BY 1 ORDER BY 2 DESC, 1"
        )
        return [(r["model_version"], int(r["n"])) for r in cur.fetchall()]


def resolve_model_version(conn: psycopg.Connection, given: str | None) -> str:
    """Use ``given``; otherwise the single model_version present in buildings_det."""
    versions = list_model_versions(conn)
    if given:
        if given not in {v for v, _ in versions}:
            raise RuntimeError(
                f"no detections with model_version={given!r}; present: "
                f"{[v for v, _ in versions] or 'none'}"
            )
        return given
    if len(versions) == 1:
        return versions[0][0]
    if not versions:
        raise RuntimeError("buildings_det is empty; run `geoqc cv predict --to-db` first")
    raise RuntimeError(f"several model versions present, pass --model-version: {versions}")


# ------------------------------------------------------------------------------------------------
# Assignment algorithms (pure Python; greedy is also implemented in SQL)
# ------------------------------------------------------------------------------------------------


def greedy_assign(pairs: Iterable[Pair]) -> list[Pair]:
    """Greedy 1:1 by IoU DESC (ties: ref_id, det_id). Mirrors ``geoqc_match_greedy``."""
    used_ref: set[int] = set()
    used_det: set[int] = set()
    out: list[Pair] = []
    for ref_id, det_id, iou in sorted(pairs, key=lambda p: (-p[2], p[0], p[1])):
        if ref_id in used_ref or det_id in used_det:
            continue
        used_ref.add(ref_id)
        used_det.add(det_id)
        out.append((ref_id, det_id, iou))
    return out


def _clusters(pairs: Sequence[Pair]) -> list[list[Pair]]:
    """Connected components of the bipartite ref/det graph (union-find)."""
    parent: dict[tuple[str, int], tuple[str, int]] = {}

    def find(x: tuple[str, int]) -> tuple[str, int]:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: tuple[str, int], b: tuple[str, int]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for ref_id, det_id, _ in pairs:
        union(("r", ref_id), ("d", det_id))
    groups: dict[tuple[str, int], list[Pair]] = {}
    for p in pairs:
        groups.setdefault(find(("r", p[0])), []).append(p)
    return list(groups.values())


def hungarian_assign(pairs: Sequence[Pair], min_iou: float = 0.0) -> list[Pair]:
    """Maximum-total-IoU 1:1 assignment per intersection cluster; keeps pairs with IoU >= min_iou."""
    out: list[Pair] = []
    for cluster in _clusters(list(pairs)):
        refs = sorted({p[0] for p in cluster})
        dets = sorted({p[1] for p in cluster})
        if len(refs) == 1 or len(dets) == 1:
            out.extend(p for p in greedy_assign(cluster)[:1] if p[2] >= min_iou)
            continue
        ri = {r: i for i, r in enumerate(refs)}
        di = {d: j for j, d in enumerate(dets)}
        iou = np.zeros((len(refs), len(dets)), dtype=float)
        for ref_id, det_id, v in cluster:
            iou[ri[ref_id], di[det_id]] = max(iou[ri[ref_id], di[det_id]], v)
        rows, cols = linear_sum_assignment(-iou)
        for i, j in zip(rows, cols, strict=True):
            if iou[i, j] > 0 and iou[i, j] >= min_iou:
                out.append((refs[i], dets[j], float(iou[i, j])))
    return sorted(out, key=lambda p: (-p[2], p[0], p[1]))


# ------------------------------------------------------------------------------------------------
# DB steps
# ------------------------------------------------------------------------------------------------


def fetch_candidate_pairs(
    conn: psycopg.Connection, model_version: str, metric_srid: int, min_iou: float
) -> list[Pair]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ref_id, det_id, iou FROM geoqc_candidate_pairs(%s, %s, %s)",
            (model_version, metric_srid, min_iou),
        )
        return [(int(r["ref_id"]), int(r["det_id"]), float(r["iou"])) for r in cur.fetchall()]


def write_matches(conn: psycopg.Connection, model_version: str, pairs: Sequence[Pair]) -> int:
    """Replace ``matches`` for the model version with ``pairs`` (1:1 enforced by the UNIQUEs)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM matches WHERE model_version = %s", (model_version,))
        if pairs:
            cur.executemany(
                "INSERT INTO matches (ref_id, det_id, iou, model_version) VALUES (%s, %s, %s, %s)",
                [(r, d, i, model_version) for r, d, i in pairs],
            )
    return len(pairs)


def match_greedy_sql(
    conn: psycopg.Connection, model_version: str, metric_srid: int, iou_match: float
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT geoqc_match_greedy(%s, %s, %s) AS n", (model_version, metric_srid, iou_match)
        )
        return int(cur.fetchone()["n"])


def classify(
    conn: psycopg.Connection,
    model_version: str,
    metric_srid: int,
    iou_match: float,
    iou_mismatch_min: float,
    aoi_geojson: str | None = None,
) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT class, n FROM geoqc_classify(%s, %s, %s, %s, %s)",
            (model_version, metric_srid, iou_match, iou_mismatch_min, aoi_geojson),
        )
        counts = {r["class"]: int(r["n"]) for r in cur.fetchall()}
    for cls in ("missing_in_map", "missing_on_image", "geometry_mismatch"):
        counts.setdefault(cls, 0)
    return counts


def has_secondary_source(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM buildings_ref WHERE source <> 'osm') AS x")
        return bool(cur.fetchone()["x"])


def score_discrepancies(
    conn: psycopg.Connection,
    model_version: str,
    metric_srid: int,
    mcfg: dict[str, Any],
    aoi_geojson: str | None = None,
) -> tuple[int, int]:
    """Compute features in SQL, confidence in Python; write both. Returns (n_scored, n_aoi_edge)."""
    has_sec = has_secondary_source(conn)
    buffer_m = float(mcfg.get("aoi_edge_buffer_m", 0.0))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM geoqc_discrepancy_features(%s, %s, %s, %s)",
            (model_version, metric_srid, aoi_geojson, buffer_m),
        )
        rows = cur.fetchall()
    feats: list[tuple[Any, ...]] = []
    n_edge = 0
    for r in rows:
        f = Features(
            cls=r["class"],
            score=r["score"],
            area_m2=float(r["area_m2"] or 0.0),
            iou=r["iou"],
            secondary_coverage=float(r["secondary_coverage"] or 0.0),
            has_secondary=has_sec,
            aoi_edge=bool(r["aoi_edge"]),
        )
        s = score_features(f, mcfg)
        n_edge += int(f.aoi_edge)
        feats.append(
            (
                r["id"],
                f.score,
                f.area_m2,
                s.area_norm,
                f.secondary_coverage,
                has_sec,
                f.iou,
                f.aoi_edge,
                s.confidence_raw,
                s.confidence,
            )
        )
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM discrepancy_features f USING discrepancies x "
            "WHERE f.discrepancy_id = x.id AND x.model_version = %s",
            (model_version,),
        )
        if feats:
            cur.executemany(
                "INSERT INTO discrepancy_features (discrepancy_id, score, area_m2, area_norm, "
                "secondary_coverage, has_secondary, max_iou_any, aoi_edge, confidence_raw, confidence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                feats,
            )
            cur.execute(
                "UPDATE discrepancies x SET confidence = f.confidence, updated_at = now() "
                "FROM discrepancy_features f WHERE f.discrepancy_id = x.id AND x.model_version = %s",
                (model_version,),
            )
    return len(feats), n_edge


def _counts(conn: psycopg.Connection, model_version: str, res: RunResult) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM buildings_ref WHERE source = 'osm'")
        res.n_ref_osm = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*) AS n FROM buildings_ref WHERE source <> 'osm'")
        res.n_ref_secondary = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n FROM buildings_det WHERE model_version = %s", (model_version,)
        )
        res.n_det = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n, avg(iou) AS mean_iou FROM matches WHERE model_version = %s",
            (model_version,),
        )
        row = cur.fetchone()
        res.n_matches = int(row["n"])
        res.mean_iou_matched = float(row["mean_iou"]) if row["mean_iou"] is not None else None


# ------------------------------------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------------------------------------


def run_matching(
    conn: psycopg.Connection,
    model_version: str | None = None,
    *,
    cfg: dict[str, Any] | None = None,
    metric_srid: int | None = None,
    algorithm: str | None = None,
    aoi_geojson: str | None = None,
    aoi_path: Path | str | None = None,
) -> RunResult:
    """Full ``geoqc matching run`` inside the caller's transaction.

    Steps: (1) split multipart detections, (2) NMS de-duplication, (3) 1:1 assignment into
    ``matches`` (greedy in SQL or Hungarian in Python), (4) classification into ``discrepancies``,
    (5) feature extraction + confidence scoring. Idempotent per model_version.
    """
    cfg = cfg or load_config()
    mcfg = cfg["matching"]
    algo = (algorithm or mcfg.get("algorithm") or "greedy").lower()
    if algo not in ALGORITHMS:
        raise ValueError(f"algorithm must be one of {ALGORITHMS}, got {algo!r}")

    mv = resolve_model_version(conn, model_version)
    srid = resolve_metric_srid(conn, cfg, metric_srid)
    if aoi_geojson is None:
        path = aoi_path if aoi_path is not None else mcfg.get("aoi_path")
        aoi_geojson = edge_cases.load_aoi_geojson(resolve_path(path)) if path else None

    res = RunResult(
        model_version=mv, metric_srid=srid, algorithm=algo, aoi_used=aoi_geojson is not None
    )
    iou_match = float(mcfg["iou_match"])
    iou_mismatch_min = float(mcfg["iou_mismatch_min"])

    if mcfg.get("split_multipart_detections", True):
        res.n_det_split = edge_cases.split_multipart_detections(conn, mv)
    if mcfg.get("dedup_detections", True):
        res.n_det_suppressed = edge_cases.dedup_detections(conn, mv, srid, float(mcfg["dedup_iou"]))
    else:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM det_suppressed WHERE model_version = %s", (mv,))

    if algo == "greedy":
        match_greedy_sql(conn, mv, srid, iou_match)
        res.n_candidates = len(fetch_candidate_pairs(conn, mv, srid, iou_match))
    else:
        pairs = fetch_candidate_pairs(conn, mv, srid, iou_match)
        res.n_candidates = len(pairs)
        write_matches(conn, mv, hungarian_assign(pairs, min_iou=iou_match))

    res.counts_by_class = classify(conn, mv, srid, iou_match, iou_mismatch_min, aoi_geojson)
    res.n_scored, res.n_aoi_edge = score_discrepancies(conn, mv, srid, mcfg, aoi_geojson)
    res.has_secondary = has_secondary_source(conn)
    _counts(conn, mv, res)
    return res


def stats(conn: psycopg.Connection, model_version: str | None = None) -> dict[str, Any]:
    """Counters for ``geoqc matching stats`` (read-only)."""
    out: dict[str, Any] = {"model_versions": list_model_versions(conn)}
    mv = model_version
    if mv is None and len(out["model_versions"]) == 1:
        mv = out["model_versions"][0][0]
    out["model_version"] = mv
    with conn.cursor() as cur:
        cur.execute("SELECT source, count(*) AS n FROM buildings_ref GROUP BY 1 ORDER BY 1")
        out["buildings_ref"] = {r["source"]: int(r["n"]) for r in cur.fetchall()}
        params: tuple[Any, ...] = (mv,) if mv else ()
        where = "WHERE model_version = %s" if mv else ""
        cur.execute(f"SELECT count(*) AS n FROM buildings_det {where}", params)
        out["buildings_det"] = int(cur.fetchone()["n"])
        cur.execute(f"SELECT count(*) AS n FROM det_suppressed {where}", params)
        out["det_suppressed"] = int(cur.fetchone()["n"])
        cur.execute(f"SELECT count(*) AS n, avg(iou) AS mean_iou FROM matches {where}", params)
        row = cur.fetchone()
        out["matches"] = int(row["n"])
        out["mean_iou_matched"] = float(row["mean_iou"]) if row["mean_iou"] is not None else None
        cur.execute(
            f"SELECT class, status, count(*) AS n, avg(confidence) AS mean_conf "
            f"FROM discrepancies {where} GROUP BY 1, 2 ORDER BY 1, 2",
            params,
        )
        by_class: dict[str, dict[str, Any]] = {}
        for r in cur.fetchall():
            c = by_class.setdefault(
                r["class"], {"total": 0, "by_status": {}, "mean_confidence": None}
            )
            c["by_status"][r["status"]] = int(r["n"])
            c["total"] += int(r["n"])
        for cls in ("missing_in_map", "missing_on_image", "geometry_mismatch"):
            by_class.setdefault(cls, {"total": 0, "by_status": {}, "mean_confidence": None})
        cur.execute(
            f"SELECT class, avg(confidence) AS mean_conf FROM discrepancies {where} GROUP BY 1",
            params,
        )
        for r in cur.fetchall():
            by_class[r["class"]]["mean_confidence"] = float(r["mean_conf"])
        out["discrepancies"] = by_class
        out["discrepancies_total"] = sum(c["total"] for c in by_class.values())
        cur.execute(
            "SELECT count(*) FILTER (WHERE f.aoi_edge) AS n_edge, count(*) AS n "
            f"FROM discrepancy_features f JOIN discrepancies x ON x.id = f.discrepancy_id {where}",
            params,
        )
        row = cur.fetchone()
        out["aoi_edge"] = int(row["n_edge"])
        out["scored"] = int(row["n"])
    return out
