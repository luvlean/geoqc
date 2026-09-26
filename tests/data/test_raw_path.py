"""Raw image path follows the image chosen in config/aoi.geojson."""

from geoqc.data.cli import _image_meta, _raw_path


def _cfg() -> dict:
    return {
        "data": {
            "image": {
                "source": "openaerialmap",
                "id": "old",
                "url": "https://example.org/old.tif",
                "raw_path": "data/raw/openaerialmap_old.tif",
            }
        }
    }


def test_raw_path_from_config_when_ids_match():
    """Путь из конфига используется, если AOI указывает на тот же снимок."""
    cfg = _cfg()
    meta = _image_meta(cfg, {})
    assert _raw_path(cfg, meta, None).name == "openaerialmap_old.tif"


def test_raw_path_follows_aoi_image():
    """Если AOI выбрал другой снимок, имя файла строится по его id, а не по конфигу."""
    cfg = _cfg()
    meta = _image_meta(cfg, {"image_id": "new", "image_url": "https://example.org/new.tif"})
    assert meta["url"].endswith("new.tif")
    assert _raw_path(cfg, meta, None).name == "openaerialmap_new.tif"
