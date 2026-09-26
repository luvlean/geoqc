"""SpaceNet loader: S3 listing, deterministic sample, download cache, chip preparation, report.

Offline: a fake HTTP session serves the listing and synthetic GeoTIFF/GeoJSON pairs that mimic
SpaceNet 2 (EPSG:4326, 2.7e-6 deg pixels, uint16 bands). One network test lists the real bucket.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin

from geoqc.cv.external import (
    external_config,
    fetch_spacenet,
    image_ids,
    list_keys,
    parse_s3_listing,
    prepare_chip,
    prepare_spacenet,
    sample_ids,
    stretch_to_uint8,
)

CITY = "AOI_3_Paris"
RES = 2.7e-6  # deg, like SpaceNet PS-RGB
LON0, LAT0 = 2.2, 49.02
SIZE = 650
NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _listing(keys: list[str], token: str | None = None) -> str:
    items = "".join(f"<Contents><Key>{k}</Key><Size>10</Size></Contents>" for k in keys)
    trunc = "true" if token else "false"
    nxt = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
    return f'<ListBucketResult xmlns="{NS}"><IsTruncated>{trunc}</IsTruncated>{nxt}{items}</ListBucketResult>'


def _square(cx_px: int, cy_px: int, half_px: int) -> dict:
    x0, x1 = LON0 + (cx_px - half_px) * RES, LON0 + (cx_px + half_px) * RES
    y0, y1 = LAT0 - (cy_px + half_px) * RES, LAT0 - (cy_px - half_px) * RES
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def write_tile(path: Path, *, nodata_rows: int = 0, bright: tuple[int, int, int] = (325, 325, 30)):
    """uint16 tile: background 300, a bright square around ``bright`` (cx, cy, half) in px."""
    arr = np.full((3, SIZE, SIZE), 300, dtype=np.uint16)
    cx, cy, h = bright
    arr[:, cy - h : cy + h, cx - h : cx + h] = 1500
    if nodata_rows:
        arr[:, SIZE - nodata_rows :, :] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", width=SIZE, height=SIZE, count=3, dtype="uint16",
        crs="EPSG:4326", transform=from_origin(LON0, LAT0, RES, RES),
    ) as dst:  # fmt: skip
        dst.write(arr)


def write_labels(path: Path, features: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), "utf-8")


def raw_pair(raw: Path, img_id: int) -> tuple[Path, Path]:
    stem = f"SN2_buildings_train_{CITY}"
    return (
        raw / CITY / "PS-RGB" / f"{stem}_PS-RGB_img{img_id}.tif",
        raw / CITY / "geojson_buildings" / f"{stem}_geojson_buildings_img{img_id}.geojson",
    )


def test_listing_parse_and_pagination():
    """XML ListObjectsV2 разбирается; пагинация идёт по continuation-token до конца."""
    items, token = parse_s3_listing(_listing(["a/img1.tif"], token="t1"))
    assert items == [("a/img1.tif", 10)] and token == "t1"
    pages = {None: _listing(["p/x_img1.tif"], "t1"), "t1": _listing(["p/x_img2.tif"])}

    class Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class Sess:
        def get(self, url, params=None, timeout=None):
            return Resp(pages[params.get("continuation-token")])

    keys = [k for k, _ in list_keys("https://bucket", "p/", Sess())]
    assert keys == ["p/x_img1.tif", "p/x_img2.tif"]


def test_sample_is_deterministic():
    """Выборка id детерминирована по seed, отсортирована; n=0 или n>len -> все id."""
    ids = image_ids([f"x/SN2_PS-RGB_img{i}.tif" for i in (10, 2, 7, 2)] + ["x/readme.txt"])
    assert ids == [2, 7, 10]
    many = list(range(100))
    assert sample_ids(many, 5, 42) == sample_ids(many, 5, 42)
    assert sample_ids(many, 5, 42) != sample_ids(many, 5, 1)
    assert sample_ids(ids, 0, 42) == ids and sample_ids(ids, 10, 42) == ids


def test_fetch_downloads_pairs_and_caches(tmp_path: Path):
    """fetch скачивает пары снимок+разметка, пишет SOURCE.json с лицензией; повтор берёт из кэша."""
    tif = io.BytesIO()
    prefix = "spacenet/SN2_buildings/train"
    keys = [
        f"{prefix}/{CITY}/PS-RGB/SN2_buildings_train_{CITY}_PS-RGB_img{i}.tif" for i in (1, 2, 3)
    ]
    tif.write(b"II*\x00fake")
    calls: list[str] = []

    class Resp:
        def __init__(self, text="", content=b""):
            self.text, self._content = text, content

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield self._content

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Sess:
        def get(self, url, params=None, stream=False, timeout=None):
            calls.append(url)
            if params:
                return Resp(text=_listing(keys))
            return Resp(content=tif.getvalue())

    cfg = {
        "external": {
            "cities": [CITY],
            "per_city": 2,
            "raw_dir": str(tmp_path / "raw"),
            "workers": 2,
        }
    }
    s1 = fetch_spacenet(cfg, session=Sess())
    assert s1["n_downloaded"] == 4 and s1["n_cached"] == 0
    files = sorted(p.name for p in (tmp_path / "raw").rglob("*") if p.is_file())
    assert len([f for f in files if f.endswith(".tif")]) == 2
    assert len([f for f in files if f.endswith(".geojson")]) == 2
    src = json.loads((tmp_path / "raw" / "SOURCE.json").read_text())
    assert src["license"] == "CC BY-SA 4.0" and src["cities"][CITY]["n_selected"] == 2
    s2 = fetch_spacenet(cfg, session=Sess())
    assert s2["n_downloaded"] == 0 and s2["n_cached"] == 4
    assert not list((tmp_path / "raw").rglob("*.part"))


def test_stretch_maps_percentiles_and_keeps_nodata_black():
    """Растяжка по перцентилям -> uint8 0..255; пиксели вне снимка остаются 0."""
    bands = np.tile(np.linspace(100, 2000, 100, dtype=np.float32), (3, 100, 1))
    valid = np.ones((100, 100), bool)
    valid[:, :10] = False
    out = stretch_to_uint8(bands, valid, (2, 98))
    assert out.dtype == np.uint8 and out.shape == (100, 100, 3)
    assert out[:, :10].max() == 0
    assert out[:, -1].min() == 255 and out[:, 11].max() < 20


def test_prepare_chip_reprojects_and_filters(tmp_path: Path):
    """Тайл -> 512x512 в UTM 0,3 м; площадь здания сохраняется; мелкие, внешние и на nodata отброшены."""
    img, lab = raw_pair(tmp_path, 5)
    write_tile(img, nodata_rows=60, bright=(325, 325, 30))
    write_labels(
        lab,
        [
            _square(325, 325, 30),  # building on the bright square
            _square(100, 330, 3),  # ~6 px -> < 12 m2
            _square(325, 625, 20),  # inside the nodata rows at the bottom
        ],
    )
    chip = prepare_chip(img, lab, CITY)
    assert chip.chip_id == f"{CITY}_img5" and chip.crs == "EPSG:32631"
    assert chip.image.shape == (512, 512, 3) and chip.image.dtype == np.uint8
    assert len(chip.buildings) == 1
    # 60 x 60 px of 2.7e-6 deg at lat 49: 60*0.3006 m by 60*0.1970 m
    expected = (60 * RES * 111_320) * (60 * RES * 111_320 * np.cos(np.radians(LAT0)))
    assert chip.buildings["area_m2"].iloc[0] == pytest.approx(expected, rel=0.05)
    assert not chip.buildings["partial"].iloc[0]
    assert chip.valid.mean() < 1.0  # black side bands: 650 px * 0.197 m < 512 px * 0.3 m


def test_prepare_spacenet_end_to_end(tmp_path: Path):
    """Сборка датасета: YOLO-seg + маски + data.yaml; тайл почти без снимка пропущен."""
    raw, out = tmp_path / "raw", tmp_path / "out"
    for i in range(1, 6):
        img, lab = raw_pair(raw, i)
        write_tile(img)
        write_labels(lab, [_square(325, 325, 30)])
    img, lab = raw_pair(raw, 9)
    write_tile(img, nodata_rows=640)
    write_labels(lab, [_square(325, 325, 30)])
    cfg = {"external": {"cities": [CITY], "raw_dir": str(raw), "out_dir": str(out)}}
    meta = prepare_spacenet(cfg)
    assert meta["n_skipped_nodata"] == 1
    assert meta["n_train"] + meta["n_holdout"] == 5 and meta["n_holdout"] >= 1
    split = pd.read_csv(out / "split.csv")
    assert set(split["city"]) == {CITY} and (split["n_instances"] == 1).all()
    for r in split.itertuples():
        lines = (out / "labels" / r.subset / f"{r.tile_id}.txt").read_text().splitlines()
        assert len(lines) == 1 and lines[0].startswith("0 ")
        coords = np.asarray(lines[0].split()[1:], float)
        assert coords.min() >= 0 and coords.max() <= 1
        mask = (out / "masks" / r.subset / f"{r.tile_id}.png").exists()
        assert mask and (out / "images" / r.subset / f"{r.tile_id}.png").exists()
    assert (out / "data.yaml").exists() and (out / "buildings.gpkg").exists()
    assert "labels/test" not in (out / "data.yaml").read_text()


def test_report_builds_figures_without_aoi(tmp_path: Path):
    """Отчёт строится и без данных AOI: рисунки SpaceNet есть, раздел AOI пустой."""
    from geoqc.cv.external_report import build_report

    raw, out = tmp_path / "raw", tmp_path / "out"
    for i in range(1, 5):
        img, lab = raw_pair(raw, i)
        write_tile(img)
        write_labels(lab, [_square(325, 325, 30)] if i != 4 else [])
    cfg = {"external": {"cities": [CITY], "raw_dir": str(raw), "out_dir": str(out)}}
    prepare_spacenet(cfg)
    pipe = {"paths": {"labels": str(tmp_path / "nolabels"), "interim": str(tmp_path / "noint")}}
    rep = build_report(
        cfg, pipe, figures_dir=tmp_path / "figs", report_path=tmp_path / "r.json", per_city=2
    )
    names = {Path(p).name for p in rep["figures"]}
    assert {"spacenet2_samples.jpg", "spacenet2_cases.jpg", "spacenet2_stats.png"} <= names
    assert rep["spacenet"][CITY]["chips"] == 4 and rep["aoi_osm"] == {}


def test_config_defaults_are_overridable():
    """cv.external поверх значений по умолчанию; источник — только train (test_public без разметки)."""
    cfg = external_config({"external": {"per_city": 7}})
    assert cfg["per_city"] == 7 and cfg["prefix"].endswith("/train")


@pytest.mark.network
def test_real_bucket_lists_paris_tiles():
    """Сеть: публичный бакет SpaceNet отдаёт список тайлов Парижа без ключей AWS."""
    cfg = external_config(None)
    keys = list_keys(cfg["base_url"], f"{cfg['prefix']}/{CITY}/PS-RGB/")
    assert len(image_ids(k for k, _ in keys)) > 1000
