"""Apply the visual review of the AI first pass on test drafts (no detector used).

Input: refined_test_ids.gpkg (GrabCut-refined Microsoft drafts, ``rid``). Edits are in metres from
the tile's south-west corner. Output: GeoPackage layer ``buildings`` with the work.gpkg schema,
every feature verified=false and a ``notes`` string telling the student what to check.
Usage: python apply_test_review.py <refined_test_ids.gpkg> <out.gpkg>
"""

from __future__ import annotations

import sys

import geopandas as gpd
import rasterio
from shapely.affinity import translate
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, "scripts/experiments")
from refine_drafts import MERGE_GAP, RAW, refine_one  # noqa: E402

NOTE_REF = "ИИ: контур уточнён по снимку 7 см (GrabCut) — проверить"
NOTE_DRAFT = "ИИ: черновик Microsoft без уточнения — проверить контур"
NOTE_ADD = "ИИ: добавлено по снимку, контур приблизительный — проверить"

DELETE = {
    "1_11": [35],  # yard element at the roof edge, not a building
    "5_17": [53],  # shrub
    "7_16": [70, 71, 72, 73, 85, 65, 68],  # trucks, tank; 65 house+greenhouse+trees, 68 partial
    "9_16": [98, 111],  # partial roofs, replaced below
    "15_12": [31, 12],  # 31 two houses + trees; 12 partial roof, replaced below
}
CLIP = {
    ("8_2", 89): Polygon([(0, 25), (23, 28.5), (26, 36), (0, 36)])
}  # shadow lobe north of the house
ADD = {
    "3_17": [
        (19, 129, 27, 137),
        (37, 127, 46, 139),
        (98, 143, 111, 151),
        (7, 112, 11, 119),
        (143, 98, 147, 104),
        (77, 90.5, 82, 94),
        (113, 88, 118, 92),
    ],
    "5_17": [(37, 7, 45, 21)],
    "7_16": [(15, 14, 24, 22), (6, 86, 66, 99)],
    "9_16": [
        (37, 77, 47, 90),
        (124, 36, 139, 52),
        (63, 108, 74, 120),
        (81, 119, 86, 124),
        (34, 109, 39, 116),
        (66, 41, 73, 46),
        (137, 75, 143, 81),
        (21, 0, 42, 13),
        (17, 112, 36, 129),
    ],
    "11_14": [(101, 118, 106, 125), (100, 103, 105, 110)],
    "15_12": [
        (138, 70, 150, 83),
        (26, 69, 49, 92),
        (55, 95, 63, 105),
        (19, 115, 34, 126),
        (33, 117, 39, 122),
        (106, 46, 117, 57),
        (34, 3, 53, 20),
    ],
}
FLAG = {  # (tile, rid) or (tile, add-index as "a<i>") -> doubt to resolve
    ("7_16", 82): "синий объект — тент/машина или сарай?",
    ("7_16", 83): "синие тенты — есть ли под ними постройка?",
    ("7_16", 80): "контур у теплиц — есть ли здание?",
    ("15_12", 26): "к дому примыкает стеклянная теплица — по протоколу её вырезать",
    ("15_12", 28): "белый объект у дороги — постройка или машина?",
    ("15_12", "a1"): "возможно, это два здания — разделить",
}
EXTRA = {
    "7_16": [
        ((16, 117, 25, 142), "ряд сараев/ангаров — разделить по крышам"),
        ((100, 107, 123, 128), "тёмная площадь — крыша ангара или площадка?"),
    ]
}


def main(refined: str, out: str) -> None:
    src = rasterio.open(RAW)
    ref = gpd.read_file(refined).to_crs(src.crs)
    tiles = gpd.read_file("data/tiles/tiles.gpkg").to_crs(src.crs).set_index("tile_id")
    rows = []
    for r in ref.itertuples():
        if r.rid in DELETE.get(r.tile_id, []):
            continue
        g = r.geometry
        x0, y0 = tiles.geometry[r.tile_id].bounds[:2]
        if (r.tile_id, r.rid) in CLIP:
            g = g.difference(translate(CLIP[(r.tile_id, r.rid)], x0, y0))
            g = max(g.geoms, key=lambda p: p.area) if g.geom_type == "MultiPolygon" else g
        note = (
            NOTE_REF
            if not r.note.startswith(
                ("draft kept", "merged 2 drafts (D-016); draft", "merged 3 drafts (D-016); draft")
            )
            else NOTE_DRAFT
        )
        if (r.tile_id, r.rid) in FLAG:
            note = f"ИИ: спорно — {FLAG[(r.tile_id, r.rid)]}"
        rows.append(
            {"tile_id": r.tile_id, "geometry": g, "source": "pre_annotation", "notes": note}
        )
    for tile, rects in ADD.items():
        x0, y0 = tiles.geometry[tile].bounds[:2]
        for i, (a, b, c, d) in enumerate(rects):
            seed = translate(box(a, b, c, d), x0, y0)
            g, how = refine_one(src, seed)
            note = f"ИИ: спорно — {FLAG[(tile, f'a{i}')]}" if (tile, f"a{i}") in FLAG else NOTE_ADD
            rows.append({"tile_id": tile, "geometry": g, "source": "manual", "notes": note})
    for tile, items in EXTRA.items():
        x0, y0 = tiles.geometry[tile].bounds[:2]
        for (a, b, c, d), why in items:
            rows.append(
                {
                    "tile_id": tile,
                    "geometry": translate(box(a, b, c, d), x0, y0),
                    "source": "manual",
                    "notes": f"ИИ: спорно — {why}",
                }
            )
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=src.crs)
    merged = []
    for tile, grp in gdf.groupby(
        "tile_id"
    ):  # sections of one building touch -> one polygon (D-016)
        blob = unary_union([g.buffer(MERGE_GAP / 2) for g in grp.geometry])
        for part in getattr(blob, "geoms", [blob]):
            m = grp[grp.geometry.buffer(MERGE_GAP / 2).intersects(part)]
            geom = unary_union(list(m.geometry)).buffer(MERGE_GAP / 2).buffer(-MERGE_GAP / 2)
            geom = (
                max(geom.geoms, key=lambda p: p.area) if geom.geom_type == "MultiPolygon" else geom
            )
            notes = sorted(set(m["notes"]), key=lambda n: ("спорно" not in n, n))
            merged.append(
                {
                    "tile_id": tile,
                    "split": "test",
                    "source": "manual" if (m["source"] == "manual").any() else "pre_annotation",
                    "verified": False,
                    "annotator": "",
                    "notes": "; ".join(notes)[:250],
                    "geometry": gpd.GeoSeries([Polygon(geom.exterior)], crs=src.crs)
                    .to_crs(4326)
                    .iloc[0]
                    .buffer(0),
                }
            )
    res = gpd.GeoDataFrame(merged, geometry="geometry", crs=4326)
    res["geometry"] = res.geometry.apply(
        lambda g: g if g.geom_type == "MultiPolygon" else gpd.GeoSeries([g]).union_all()
    )
    res.to_file(out, driver="GPKG", layer="buildings")
    print(len(res), res.groupby("tile_id").size().to_dict())
    print(res["notes"].str.split(";").str[0].str[:40].value_counts().to_dict())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
