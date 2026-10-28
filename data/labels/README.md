# data/labels — ручной эталон зданий

Эталон для метрик детекции. Как размечать — [docs/annotation/INSTRUCTION_QGIS.md](../../docs/annotation/INSTRUCTION_QGIS.md).

```
data/labels/
  tiles_split.csv            tile_id, split, stratum, row, col       (select-tiles, add-test-tiles)
  recheck_tiles.csv          тайлы слепой повторной разметки для G2  (select-tiles)
  select_tiles_report.json   диагностика отбора                       (select-tiles)
  train/ | val/ | test/
    draft.geojson            черновик, verified=false                 (pre-annotate)
    work.gpkg                рабочий файл QGIS: buildings, recheck    (qgis-project, правит человек)
    qgis/                    проект .qgz, стили .qml, рамки тайлов    (qgis-project)
    buildings.geojson        итоговый эталон, verified=true           (finalize)
    recheck.geojson          повторная разметка                       (finalize)
    finalize_summary.json    что изменил finalize                     (finalize)
```

Статусы готовности сплита:

| Статус | Признак |
|---|---|
| не начат | есть только `draft.geojson` |
| в работе | есть `work.gpkg`, в нём есть `verified=false` |
| выгружен | есть `buildings.geojson` |
| принят | `reports/annotation.json` → `splits.<split>.status = accepted` |

Наборы (D-021): `train` — 11 тайлов, на них дообучается модель; `val` — 13 тайлов, на них подбираются параметры и порог
и считается G3; `test` — 10 новых тайлов (`add-test-tiles`), оценивается один раз финальной моделью (G4/G5).
Модуль `geoqc.cv` и ноутбук обучения каталог `test` не читают.
Проекты `.qgz` в git не попадают: в них может оказаться ключ `ARCGIS_API_KEY`.
