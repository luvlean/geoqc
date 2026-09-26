"""Тесты выбора UTM-зоны (docs/CONTRACTS.md §3.1)."""

from __future__ import annotations

import pytest

from geoqc.data.crs import utm_epsg_for, utm_epsg_for_bbox


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [
        (30.40, 59.89, 32636),  # Санкт-Петербург, северное полушарие, зона 36
        (37.62, 55.75, 32637),  # Москва
        (-74.0, 40.7, 32618),  # Нью-Йорк, западная долгота
        (151.2, -33.9, 32756),  # Сидней, южное полушарие -> 327xx
        (-58.4, -34.6, 32721),  # Буэнос-Айрес
        (0.0, 0.0, 32631),  # граница зон 30/31: нулевой меридиан относится к зоне 31
        (-180.0, 10.0, 32601),  # западный край
        (180.0, 10.0, 32660),  # восточный край ограничен зоной 60
    ],
)
def test_utm_epsg_for_hemispheres_and_zones(lon: float, lat: float, epsg: int) -> None:
    """Для точек в обоих полушариях и на границах зон возвращается ожидаемый код EPSG."""
    assert utm_epsg_for(lon, lat) == epsg


def test_utm_epsg_for_bbox_uses_centre() -> None:
    """Зона для bbox берётся по его центру, а не по углам."""
    assert utm_epsg_for_bbox((29.0, 59.0, 31.0, 60.0)) == 32636
    assert utm_epsg_for_bbox((-1.0, -10.0, 1.0, -5.0)) == 32731


def test_utm_epsg_for_rejects_out_of_range() -> None:
    """Координаты вне допустимого диапазона вызывают ValueError."""
    with pytest.raises(ValueError):
        utm_epsg_for(200.0, 10.0)
    with pytest.raises(ValueError):
        utm_epsg_for(10.0, 95.0)
