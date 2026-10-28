# Глава 6. Экспериментальная оценка

Все числа — из `reports/metrics.json` (схема `docs/CONTRACTS.md` §3.9), сгенерированного `geoqc eval gates` на коммите `{{metrics.git_commit}}` (`{{metrics.generated_at}}`, незакоммиченные изменения кода: {{metrics.git_dirty}}); подстановка — `scripts/render_metrics_table.py fill`. Определения метрик — `docs/metrics_and_formulas.md`. Пороги гейтов зафиксированы до получения результатов на `test` (`config/quality_gates.yaml`).

## 6.1 Методика

- Сплиты (D-021): `train` (11 тайлов, 126 зданий) — дообучение; `val` (`{{metrics.dataset.n_tiles_val}}` тайлов, `{{metrics.dataset.n_gt_val}}` зданий) — ранняя остановка, выбор модели и порога уверенности; `test` (`{{metrics.dataset.n_tiles_test}}` тайлов, `{{metrics.dataset.n_gt_test}}` зданий) — однократная оценка.
- Детекции обрезаются по тайлам сплита; сопоставление 1:1 с эталоном (`config/eval.yaml → match_method`), пара засчитывается при IoU ≥ 0.5.
- Модель `{{metrics.model.model_version}}`, порог `{{metrics.model.conf_threshold}}` (подобран на `val`, `reports/threshold_sweep_val.json`).
- Расхождения верифицированы вручную на стратифицированной выборке (`reports/verification_sample.csv` → `reports/verification.csv`, вердикты `map_error / model_error / image_issue / unsure`).

## 6.2 Данные и разметка (G1, G2)

| Показатель | Значение | Ключ |
|---|---|---|
| Медианное смещение снимок/вектор, м | `{{metrics.georeference.median_offset_m}}` (на `{{metrics.georeference.n_control_points}}` точках) | `georeference.median_offset_m` |
| Согласие разметки, IoU / F1 | `{{metrics.annotation.agreement_iou}}` / `{{metrics.annotation.agreement_f1}}` (`{{metrics.annotation.n_checked}}` объектов) | `annotation.*` |

G1 (≤ 2 м): `{{metrics.gates.G1.passed}}`; G2 (≥ 0.80): `{{metrics.gates.G2.passed}}`. Примечания: `{{metrics.gates.G1.note}}`; `{{metrics.gates.G2.note}}`.

## 6.3 Качество детекции (G3–G5)

| Сплит | TP | FP | FN | Precision | Recall | F1 | mean IoU |
|---|---|---|---|---|---|---|---|
| val | `{{metrics.detection.val.tp}}` | `{{metrics.detection.val.fp}}` | `{{metrics.detection.val.fn}}` | `{{metrics.detection.val.precision}}` | `{{metrics.detection.val.recall}}` | `{{metrics.detection.val.f1}}` | `{{metrics.detection.val.mean_iou_matched}}` |
| test | `{{metrics.detection.test.tp}}` | `{{metrics.detection.test.fp}}` | `{{metrics.detection.test.fn}}` | `{{metrics.detection.test.precision}}` | `{{metrics.detection.test.recall}}` | `{{metrics.detection.test.f1}}` | `{{metrics.detection.test.mean_iou_matched}}` |

Гейты: G3 (F1 val ≥ 0.70) — `{{metrics.gates.G3.passed}}`; G4 (F1 test ≥ 0.70) — `{{metrics.gates.G4.passed}}`; G5 (mean IoU test ≥ 0.60) — `{{metrics.gates.G5.passed}}`. Разрыв между `val` и `test` интерпретируется как оценка переобучения порога/дообучения на `val`.

Рисунки: кривая F1 от порога — `reports/threshold_sweep_val.json`; гистограмма IoU совпавших пар и примеры тайл+эталон+детекции — `reports/figures/` (генерация `geoqc eval error-analysis`).

## 6.4 Очередь расхождений (G6, G7)

Число расхождений по классам (`discrepancies.counts_by_class`): `missing_in_map` — `{{metrics.discrepancies.counts_by_class.missing_in_map}}`, `missing_on_image` — `{{metrics.discrepancies.counts_by_class.missing_on_image}}`, `geometry_mismatch` — `{{metrics.discrepancies.counts_by_class.geometry_mismatch}}`.

Ручная верификация (`discrepancies.verified_by_class`):

| Класс | Проверено | Реальных ошибок карты | Precision |
|---|---|---|---|
| `missing_in_map` | `{{metrics.discrepancies.verified_by_class.missing_in_map.n}}` | `{{metrics.discrepancies.verified_by_class.missing_in_map.true_error}}` | `{{metrics.discrepancies.verified_by_class.missing_in_map.precision}}` |
| `missing_on_image` | `{{metrics.discrepancies.verified_by_class.missing_on_image.n}}` | `{{metrics.discrepancies.verified_by_class.missing_on_image.true_error}}` | `{{metrics.discrepancies.verified_by_class.missing_on_image.precision}}` |
| `geometry_mismatch` | `{{metrics.discrepancies.verified_by_class.geometry_mismatch.n}}` | `{{metrics.discrepancies.verified_by_class.geometry_mismatch.true_error}}` | `{{metrics.discrepancies.verified_by_class.geometry_mismatch.precision}}` |
| **Всего** | — | — | `{{metrics.discrepancies.precision_overall}}` |

Precision@`{{metrics.discrepancies.precision_at_n.n}}` очереди по убыванию `confidence`: `{{metrics.discrepancies.precision_at_n.value}}`. Гейты: G6 (overall ≥ 0.70) — `{{metrics.gates.G6.passed}}`; G7 (precision@20 ≥ 0.75) — `{{metrics.gates.G7.passed}}`.

Интерпретация по классам (заполняется по `reports/verification.csv` и `reports/error_analysis.md`): доля `image_issue` и `model_error` в `missing_on_image` показывает, насколько класс загрязнён тенями/деревьями и разновременностью; precision `geometry_mismatch` отражает качество контуров модели против устаревших контуров карты.

## 6.5 Сравнение с альтернативным источником

Второй источник (`{{metrics.comparison.secondary_source}}`) как «детектор» против ручной разметки `test`: precision `{{metrics.comparison.secondary_vs_gt.precision}}`, recall `{{metrics.comparison.secondary_vs_gt.recall}}`, F1 `{{metrics.comparison.secondary_vs_gt.f1}}` — против F1 `{{metrics.comparison.detection_vs_gt.f1}}` собственной детекции на тех же тайлах (п. 6.3). Согласие модели и второго источника между собой: F1 `{{metrics.comparison.detection_vs_secondary.f1}}`. Вывод о целесообразности fallback плана (использовать готовые контуры как детектор) делается по этой паре чисел.

## 6.6 Анализ ошибок

`reports/error_analysis.md` (генерируется кодом, без гипотез там, где нет данных): FP/FN по тайлам и стратам, распределение площадей ошибок, гистограмма IoU, топ-10 худших тайлов, примеры в `reports/figures/`. Разделы: «Тени», «Сложные крыши», «Плотная застройка», «Растительность», «Граница тайла». В записку переносятся таблицы и 3–5 рисунков с подписями «путь к файлу, сплит, `model_version`».

## 6.7 Воспроизводимость и тесты (G8, G9)

`make all` в чистом окружении: `{{metrics.reproducibility.make_all_passed}}` (G8 — `{{metrics.gates.G8.passed}}`). Тесты: `{{metrics.tests.passed}}`, всего `{{metrics.tests.n_tests}}` (G9 — `{{metrics.gates.G9.passed}}`). Отчёт QA — `docs/qa/`.

## 6.8 Сводка по гейтам

| Гейт | Метрика | Цель | Факт | Пройден | Примечание |
|---|---|---|---|---|---|
| G1 | смещение, м | ≤ 2.0 | `{{metrics.gates.G1.actual}}` | `{{metrics.gates.G1.passed}}` | `{{metrics.gates.G1.note}}` |
| G2 | согласие разметки IoU | ≥ 0.80 | `{{metrics.gates.G2.actual}}` | `{{metrics.gates.G2.passed}}` | `{{metrics.gates.G2.note}}` |
| G3 | F1 val | ≥ 0.70 | `{{metrics.gates.G3.actual}}` | `{{metrics.gates.G3.passed}}` | `{{metrics.gates.G3.note}}` |
| G4 | F1 test | ≥ 0.70 | `{{metrics.gates.G4.actual}}` | `{{metrics.gates.G4.passed}}` | `{{metrics.gates.G4.note}}` |
| G5 | mean IoU test | ≥ 0.60 | `{{metrics.gates.G5.actual}}` | `{{metrics.gates.G5.passed}}` | `{{metrics.gates.G5.note}}` |
| G6 | precision расхождений | ≥ 0.70 | `{{metrics.gates.G6.actual}}` | `{{metrics.gates.G6.passed}}` | `{{metrics.gates.G6.note}}` |
| G7 | precision@20 | ≥ 0.75 | `{{metrics.gates.G7.actual}}` | `{{metrics.gates.G7.passed}}` | `{{metrics.gates.G7.note}}` |
| G8 | `make all` | true | `{{metrics.gates.G8.actual}}` | `{{metrics.gates.G8.passed}}` | `{{metrics.gates.G8.note}}` |
| G9 | pytest | true | `{{metrics.gates.G9.actual}}` | `{{metrics.gates.G9.passed}}` | `{{metrics.gates.G9.note}}` |

Для каждого непройденного гейта — абзац: фактическое значение, причина (по анализу ошибок), применённый fallback (план §8) и что потребуется для прохождения.
