# Список источников и приложения

## Список источников

Только проверяемые источники: официальные страницы, репозитории, публикации. Идентификаторы arXiv указаны там, где они известны достоверно; DOI не приводятся, чтобы исключить ошибочные. Перед сдачей оформить по требованиям кафедры (ГОСТ Р 7.0.100–2018) и проверить каждую ссылку.

1. Open Data Commons Open Database License (ODbL) v1.0. — URL: https://opendatacommons.org/licenses/odbl/1-0/
2. OpenStreetMap Wiki. Automated Edits code of conduct. — URL: https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct
3. OpenStreetMap Wiki. Quality assurance. — URL: https://wiki.openstreetmap.org/wiki/Quality_assurance (обзор инструментов Osmose, KeepRight, OSM Inspector)
4. MapRoulette — микрозадачи для улучшения OpenStreetMap. — URL: https://maproulette.org ; документация: https://learn.maproulette.org
5. Microsoft. GlobalMLBuildingFootprints. — URL: https://github.com/microsoft/GlobalMLBuildingFootprints (лицензия данных ODbL)
6. Google Research. Open Buildings. — URL: https://sites.research.google/open-buildings/
7. Sirko W., Kashubin S., Ritter M., Annkah A., Bouchareb Y. S. E., Dauphin Y., Keysers D., Neumann M., Cisse M., Quinn J. Continental-Scale Building Detection from High Resolution Satellite Imagery. 2021. — arXiv:2107.12283
8. Ronneberger O., Fischer P., Brox T. U-Net: Convolutional Networks for Biomedical Image Segmentation // MICCAI 2015. — arXiv:1505.04597
9. Jocher G., Chaurasia A., Qiu J. Ultralytics YOLOv8 (software). 2023. — URL: https://github.com/ultralytics/ultralytics (лицензия AGPL-3.0); документация: https://docs.ultralytics.com
10. Kirillov A., Mintun E., Ravi N., Mao H., Rolland C., Gustafson L., Xiao T., Whitehead S., Berg A. C., Lo W.-Y., Dollár P., Girshick R. Segment Anything. 2023. — arXiv:2304.02643 ; репозиторий: https://github.com/facebookresearch/segment-anything
11. Maggiori E., Tarabalka Y., Charpiat G., Alliez P. Can Semantic Labeling Methods Generalize to Any City? The Inria Aerial Image Labeling Benchmark // IEEE IGARSS 2017. — URL: https://project.inria.fr/aerialimagelabeling/
12. Van Etten A., Lindenbaum D., Bacastow T. M. SpaceNet: A Remote Sensing Dataset and Challenge Series. 2018. — arXiv:1807.01232 ; URL: https://spacenet.ai
13. Everingham M., Van Gool L., Williams C. K. I., Winn J., Zisserman A. The PASCAL Visual Object Classes (VOC) Challenge // International Journal of Computer Vision. 2010. Vol. 88, No. 2. P. 303–338.
14. Douglas D. H., Peucker T. K. Algorithms for the Reduction of the Number of Points Required to Represent a Digitized Line or its Caricature // Cartographica. 1973. Vol. 10, No. 2. P. 112–122.
15. Kuhn H. W. The Hungarian Method for the Assignment Problem // Naval Research Logistics Quarterly. 1955. Vol. 2. P. 83–97.
16. PostGIS — Spatial and Geographic Objects for PostgreSQL. Documentation. — URL: https://postgis.net/documentation/
17. OpenAerialMap. — URL: https://openaerialmap.org (снимки под CC-BY 4.0)
18. Maxar Open Data Program. — URL: https://www.maxar.com/open-data (лицензия CC BY-NC 4.0)
19. Overpass API. — URL: https://wiki.openstreetmap.org/wiki/Overpass_API
20. rasterio — Geospatial raster I/O for Python. — URL: https://rasterio.readthedocs.io
21. Shapely — Manipulation and analysis of geometric objects. — URL: https://shapely.readthedocs.io
22. FastAPI. — URL: https://fastapi.tiangolo.com
23. Leaflet — a JavaScript library for interactive maps. — URL: https://leafletjs.com
24. Iakubovskii P. Segmentation Models PyTorch (software). — URL: https://github.com/qubvel-org/segmentation_models.pytorch
25. Esri. ArcGIS Location Platform — Terms of Use (использование World Imagery как подложки). — URL: https://www.esri.com/en-us/legal/terms/full-master-agreement

Внутренние документы проекта: `docs/CONTRACTS.md`, `DECISIONS.md`, `docs/metrics_and_formulas.md`, `models/MODEL_CARD.md`, `data/manifest.json`, `reports/metrics.json`.

## Приложение А. Схема базы данных

Источник — `sql/001_schema.sql` (буквально контракт; воспроизводится в записке без изменений). Кратко:

| Таблица | Ключевые поля | Индексы |
|---|---|---|
| `buildings_ref` | `id`, `osm_id`, `geom MultiPolygon 4326`, `tags jsonb`, `source ∈ {osm, microsoft, google}`, `snapshot_date` | GiST(`geom`), `source` |
| `buildings_det` | `id`, `geom`, `score ∈ [0,1]`, `model_version`, `tile_id` | GiST(`geom`), `model_version` |
| `matches` | `ref_id → buildings_ref`, `det_id → buildings_det`, `iou`, `model_version`; UNIQUE(`ref_id`,`model_version`), UNIQUE(`det_id`,`model_version`) | — |
| `discrepancies` | `class ∈ {missing_in_map, missing_on_image, geometry_mismatch}`, `ref_id`, `det_id`, `iou`, `confidence ∈ [0,1]`, `geom`, `status ∈ {new, confirmed, rejected}`, `model_version`, `created_at`, `updated_at` | GiST(`geom`), (`class`, `confidence DESC`), `status` |

ER-диаграмма — `docs/architecture.md` §4. Функции сопоставления — `sql/002_matching.sql`.

## Приложение Б. Примеры расхождений

По одному–двум примерам на класс из `reports/discrepancies.geojson` и `reports/figures/`: фрагмент снимка, контур OSM, контур детекции, `confidence`, `iou`, вердикт верификации из `reports/verification.csv`. Подпись: `id` расхождения, класс, `model_version`, путь к рисунку. Заполняется после интеграции.

## Приложение В. Команды воспроизведения

```bash
scripts/bootstrap.sh && cp -n .env.example .env
make up db-init
make data labels-prep          # → правка разметки в QGIS (docs/annotation/INSTRUCTION_QGIS.md)
make detect match
geoqc eval verification-sample # → reports/verification.csv (ручная верификация)
make eval demo
```

Полный перечень CLI — `docs/CONTRACTS.md` §6; `make help`.

## Приложение Г. Финальный чек-лист (план §12)

- [ ] `make all` воспроизводит пайплайн в чистом окружении — `{{metrics.reproducibility.make_all_passed}}`
- [ ] Гейты G1–G9 проверены, фактические значения в `reports/metrics.json`
- [ ] Тестовая разметка не использовалась при обучении и подборе порогов (подтверждено QA)
- [ ] Анализ ошибок с примерами по каждому классу — `reports/error_analysis.md`
- [ ] Лицензии и атрибуция всех источников указаны (`data/manifest.json`, README)
- [ ] Демо работает: зона → расхождения → статус → экспорт
- [ ] Записка и презентация согласованы с `reports/` (`render_metrics_table.py fill` без `missing key`)
- [ ] `DECISIONS.md` содержит все отклонения от плана
- [ ] Студент самостоятельно прогнал демо и может объяснить каждый этап
