# Метрики и формулы geoqc

Базовые определения — `docs/CONTRACTS.md` §4; здесь они раскрыты с обозначениями, единицами и примерами. Все числовые примеры ниже — **синтетические иллюстрации формул**, не результаты проекта. Результаты — только в `reports/` (см. §12).

## 0. Обозначения и единицы

| Символ | Смысл | Единицы / CRS |
|---|---|---|
| $A, B$ | полигоны (MultiPolygon) эталона и детекции | геометрия в метрической UTM-зоне AOI (`config/pipeline.yaml → crs.metric`) |
| $\operatorname{area}(\cdot)$ | площадь | м² |
| $G = \{g_i\}$ | эталонные объекты (ground truth: `data/labels/<split>/buildings.geojson`, `verified=true`) | EPSG:4326 → UTM при расчёте |
| $D = \{d_j\}$ | детекции (`data/interim/detections.geojson`, `buildings_det`) | то же |
| $s_j \in [0,1]$ | уверенность модели для $d_j$ (`score`) | — |
| $\tau$ | порог IoU для засчитывания пары; $\tau = 0.5$ (`config/eval.yaml → iou_threshold`, `config/matching.yaml → iou_match`) | — |
| $R$ | объекты OSM (`buildings_ref`, `source='osm'`) | EPSG:4326 → UTM |
| $S$ | второй источник (`buildings_ref`, `source ∈ {microsoft, google}`) | то же |

Все площади, IoU, расстояния, буферы и допуски считаются в метрической CRS; хранение — EPSG:4326 (CONTRACTS §3.1).

## 1. IoU

$$
\mathrm{IoU}(A,B) = \frac{\operatorname{area}(A \cap B)}{\operatorname{area}(A \cup B)}
= \frac{\operatorname{area}(A \cap B)}{\operatorname{area}(A) + \operatorname{area}(B) - \operatorname{area}(A \cap B)} \in [0,1].
$$

Свойства: $\mathrm{IoU}=1$ при совпадении, $0$ без пересечения; для MultiPolygon берётся площадь объединения частей. При $\operatorname{area}(A\cup B)=0$ IoU считается 0.

Пример: квадраты $10\times10$ м, второй сдвинут на 5 м по $x$: пересечение $5\cdot10=50$ м², объединение $100+100-50=150$ м², $\mathrm{IoU}=50/150=1/3$. Сдвиг на 2 м: $80/120 = 2/3$. Сдвиг на 10 м: $0$.

В SQL: `geoqc_iou(geom_a, geom_b, metric_srid)` = `ST_Area(ST_Intersection(...)) / ST_Area(ST_Union(...))` после `ST_Transform(·, metric_srid)`.

## 2. Сопоставление 1:1

Дано $G$ и $D$ на одном сплите (детекции обрезаются по тайлам сплита из `tiles_split.csv`). Матрица $M_{ij} = \mathrm{IoU}(g_i, d_j)$, кандидаты — только пересекающиеся пары (GiST / `ST_Intersects`).

**Жадный алгоритм** (`algorithm: greedy`): отсортировать пары $(i,j)$ по убыванию $M_{ij}$; идти сверху, брать пару, если ни $g_i$, ни $d_j$ ещё не заняты и $M_{ij} \ge \tau$.

**Венгерский алгоритм** (`algorithm: hungarian`): на каждой компоненте связности графа пересечений решить задачу о назначениях $\max \sum_{(i,j)\in\pi} M_{ij}$ (`scipy.optimize.linear_sum_assignment` на $1 - M$), затем отбросить назначенные пары с $M_{ij} < \tau$.

Различие видно на конфликте. Пример: $g_1$ пересекается с $d_1$ (IoU 0.60) и $d_2$ (IoU 0.55); $g_2$ пересекается только с $d_1$ (IoU 0.58). Жадный: берёт $(g_1,d_1)=0.60$, затем $(g_2,\cdot)$ — $d_1$ занят → $g_2$ без пары; сумма 0.60, одна пара. Венгерский: $(g_1,d_2)+(g_2,d_1)=0.55+0.58=1.13$, две пары. Оба варианта покрыты тестами `tests/eval`, `tests/matching`.

Результат сопоставления — множество пар $P = \{(i,j)\}$ с $M_{ij}\ge\tau$.

## 3. TP / FP / FN, Precision, Recall, F1

$$
\mathrm{TP} = |P|,\qquad \mathrm{FP} = |D| - |P|,\qquad \mathrm{FN} = |G| - |P|,
$$
$$
\mathrm{Precision} = \frac{\mathrm{TP}}{\mathrm{TP}+\mathrm{FP}},\qquad
\mathrm{Recall} = \frac{\mathrm{TP}}{\mathrm{TP}+\mathrm{FN}},\qquad
\mathrm{F1} = \frac{2\,\mathrm{P}\,\mathrm{R}}{\mathrm{P}+\mathrm{R}} = \frac{2\,\mathrm{TP}}{2\,\mathrm{TP}+\mathrm{FP}+\mathrm{FN}}.
$$

Крайние случаи: $|D|=0 \Rightarrow$ Precision не определена → 0, Recall $=0$; $|G|=0$ и $|D|=0 \Rightarrow$ все три $=0$ (по соглашению кода `eval`); знаменатель 0 → 0.

Пример: $|G|=12$, $|D|=10$, пар $|P|=8$: TP $=8$, FP $=2$, FN $=4$; $P=0.80$, $R=8/12\approx0.667$, $F1 = 16/(16+2+4)\approx 0.727$.

Ключи: `detection.<split>.{tp,fp,fn,precision,recall,f1}`; гейты G3 (`val`) и G4 (`test`): $F1 \ge 0.70$.

## 4. Mean IoU сопоставленных

$$
\overline{\mathrm{IoU}} = \frac{1}{|P|}\sum_{(i,j)\in P} M_{ij}, \qquad P=\varnothing \Rightarrow 0.
$$

Пример: пары с IoU $0.9, 0.7, 0.5 \Rightarrow 0.70$. Ключ `detection.<split>.mean_iou_matched`; гейт G5 на `test`: $\ge 0.60$.

## 5. Классы расхождений

Сопоставляются $R$ (OSM) и $D$ (детекции) тем же алгоритмом §2, пороги — `config/matching.yaml`: `iou_match` $=0.5$, `iou_mismatch_min` $=0.1$. Пусть $m(x)$ — максимальный IoU объекта $x$ с любым объектом противоположного множества, $(r,d)$ — лучшая пара.

| Класс | Условие | Геометрия задачи | Таблица |
|---|---|---|---|
| `match` | $\mathrm{IoU}(r,d)\ge0.5$, оба свободны | — | `matches` |
| `geometry_mismatch` | $0.1 \le \mathrm{IoU}(r,d) < 0.5$, ни $r$, ни $d$ не в `matches` | $r \cup d$ (или по контракту модуля) | `discrepancies` |
| `missing_in_map` | $d$ без пары, $m(d) < 0.1$ | $d$ | `discrepancies` |
| `missing_on_image` | $r$ без пары | $r$ | `discrepancies` |

Предобработка (`edge_cases.py`, SQL-функции `sql/002_matching.sql`): детекции с попарным IoU $>$ `dedup_iou` $=0.7$ подавляются (NMS; подавленные id — в `det_suppressed`, строки `buildings_det` сохраняются); мультиполигональные детекции разбиваются по частям (`split_multipart_detections`); объект, не покрытый AOI, сжатой на `aoi_edge_buffer_m` $=10$ м, получает флаг `aoi_edge` → множитель `aoi_edge_factor` в §6.

Пример: квадраты §1 — сдвиг 0 м → `match` (IoU 1); сдвиг 5 м → `geometry_mismatch` (1/3); сдвиг 10 м → пересечения нет: детекция → `missing_in_map`, объект OSM → `missing_on_image`.

## 6. Уверенность расхождения (confidence)

Реализация — `src/geoqc/matching/scoring.py` (чистые функции; воспроизводится на числах в `tests/matching/test_scoring.py`), параметры — `config/matching.yaml`; формула сверена с `src/geoqc/matching/README.md` («Формулы»). Признаки каждого расхождения сохраняются в таблице `discrepancy_features` (`sql/002_matching.sql`) для анализа и воспроизводимости.

Признаки (каждый приводится к $[0,1]$ функцией $\operatorname{clip}$):

| Признак | Определение | Поле `Features` |
|---|---|---|
| $s$ | уверенность модели детекции (`buildings_det.score`); у `missing_on_image` отсутствует | `score` |
| $a_{\mathrm{norm}}$ | $\operatorname{clip}\!\left(\dfrac{\ln(\mathrm{area}/a_{\min})}{\ln(a_{\max}/a_{\min})},\,0,\,1\right)$, $a_{\min}$ = `min_area_m2` (20 м²), $a_{\max}$ = `max_area_m2` (5000 м²); площадь $\le 0 \Rightarrow 0$ | `area_m2` |
| $\mathrm{cov}_S$ | $\operatorname{area}(x \cap S)/\operatorname{area}(x)$ — доля площади объекта задачи, покрытая вторым источником $S$ | `secondary_coverage` |
| $\mathrm{IoU}$ | IoU лучшей пары (`geometry_mismatch`) либо максимальный IoU с любым объектом противоположного множества (`missing_*`) | `iou` |
| $e$ | флаг границы AOI (объект вне AOI, сжатой на `aoi_edge_buffer_m`) | `aoi_edge` |

Формулы по классам; веса $w$ — `weights.<class>` конфига, **нормированы на сумму 1 внутри класса**:

$$
\begin{aligned}
c_{\texttt{missing\_in\_map}} &= w_{s}\, s + w_{a}\, a_{\mathrm{norm}} + w_{S}\, \mathrm{cov}_S,\\
c_{\texttt{missing\_on\_image}} &= w_{a}\, a_{\mathrm{norm}} + w_{S}\,(1-\mathrm{cov}_S) + w_{c}\,(1 - \mathrm{IoU}_{\max}),\\
c_{\texttt{geometry\_mismatch}} &= w_{s}\, s + w_{\mathrm{iou}}\,(1-\mathrm{IoU}) + w_{a}\, a_{\mathrm{norm}},
\end{aligned}
\qquad
\mathrm{confidence} = \operatorname{clip}\big(c \cdot f_e\big),\quad
f_e = \begin{cases}\texttt{aoi\_edge\_factor} = 0.5, & e\\ 1, & \text{иначе.}\end{cases}
$$

Текущие веса (`config/matching.yaml`): `missing_in_map` — score 0.5, area 0.2, secondary 0.3; `missing_on_image` — area 0.3, secondary 0.4, context 0.3; `geometry_mismatch` — score 0.4, iou 0.4, area 0.2. Если второй источник не загружен (нет `buildings_ref.source ∈ {microsoft, google}`), член `secondary` исключается, остальные веса перенормируются на сумму 1. Веса подбираются только на `val`.

Смысл: крупные объекты надёжнее и важнее ($a_{\mathrm{norm}}$); для пропуска на карте подтверждение вторым источником повышает уверенность, для «снесённого» — его отсутствие там; если у объекта OSM всё же есть детекция рядом ($\mathrm{IoU}_{\max}>0$), вероятнее смещение, чем снос; для `geometry_mismatch` чем меньше IoU, тем грубее расхождение.

Пример (синтетический): `missing_in_map`, $s=0.9$, area $=316$ м² $\Rightarrow a_{\mathrm{norm}} = \ln 15.8/\ln 250 \approx 0.50$, $\mathrm{cov}_S=1$: $c = 0.5\cdot0.9 + 0.2\cdot0.5 + 0.3\cdot1 = 0.85$. Без второго источника веса $\to \{0.714, 0.286\}$: $c \approx 0.714\cdot0.9 + 0.286\cdot0.5 \approx 0.79$. Тот же объект у границы AOI: $0.85\cdot0.5 = 0.425$.

Ключ в очереди: `discrepancies.confidence`; API и веб сортируют по `confidence DESC`.

## 7. Precision расхождений (ручная верификация)

Студент заполняет `reports/verification.csv` (`id,class,confidence,iou,lon,lat,verdict,comment`, `verdict ∈ {map_error, model_error, image_issue, unsure}`). Для класса $k$ с $n_k$ проверенных и $u_k$ вердиктов `unsure`:

$$
\mathrm{Prec}_k = \frac{\#\{\texttt{map\_error}\}_k}{n_k - u_k},\qquad
\mathrm{Prec}_{\mathrm{overall}} = \frac{\sum_k \#\{\texttt{map\_error}\}_k}{\sum_k (n_k - u_k)}.
$$

`model_error` и `image_issue` — ложные срабатывания очереди (не ошибки карты). Пример: класс проверен на 20 объектах, 14 `map_error`, 3 `model_error`, 1 `image_issue`, 2 `unsure` → $14/18\approx0.778$.

Ключи: `discrepancies.verified_by_class.<class>.{n,true_error,precision}`, `discrepancies.precision_overall`; гейт G6: overall $\ge0.70$, по классам — в отчёте.

## 8. Precision@N

Среди проверенных расхождений (`reports/verification.csv`) взять топ-$N$ по `confidence` (все классы вместе):

$$
\mathrm{Prec@}N = \frac{\#\{\texttt{map\_error}\}_{\text{топ-}N}}{N - \#\{\texttt{unsure}\}_{\text{топ-}N}},\qquad
\mathrm{Prec@}N^{\,\mathrm{incl}} = \frac{\#\{\texttt{map\_error}\}_{\text{топ-}N}}{N}.
$$

Основное значение (`value`) исключает `unsure` из знаменателя — так же, как в §7; консервативный вариант `value_incl_unsure` считает `unsure` не-ошибкой (реализация `geoqc.eval.discrepancy_metrics.verification_metrics`). $N$ — `config/eval.yaml → precision_at_n` (`[10, 20, 50]`); в `metrics.json → discrepancies.precision_at_n` попадает `gate_precision_at_n` $=20$ (гейт G7, $\ge 0.75$). Пример: топ-20 содержит 16 `map_error` и 2 `unsure` → `value` $= 16/18 \approx 0.889$, `value_incl_unsure` $= 0.80$.

## 9. Согласие разметки (G2)

Реализация — `geoqc.annotation.validate.agreement_metrics`, параметры `config/annotation.yaml → validate`. На тайлах слепой повторной разметки (`recheck_fraction` = 10 %, не менее `recheck_min_tiles` = 2) объекты первой ($G$) и повторной ($G'$) разметки сопоставляются жадно 1:1 по убыванию IoU; кандидаты — пары с IoU $>$ `pair_min_iou` ($0.0$, т.е. любое пересечение):

$$
\mathrm{agreement\_iou} = \frac{1}{|P'|}\sum_{(i,j)\in P'} \mathrm{IoU}(g_i, g'_j),\qquad
\mathrm{agreement\_f1} = \frac{2\,\mathrm{TP}}{2\,\mathrm{TP}+\mathrm{FP}+\mathrm{FN}},\quad
\mathrm{TP} = \#\{(i,j)\in P' : \mathrm{IoU} \ge \texttt{iou\_match}\},
$$

$\mathrm{FP} = |G'| - \mathrm{TP}$, $\mathrm{FN} = |G| - \mathrm{TP}$, `iou_match` $=0.5$. Пример: два квадрата $10\times10$ м, второй сдвинут на 5 м → одна пара с IoU $1/3$: `agreement_iou` $=1/3$, TP $=0$, `agreement_f1` $=0$ (тест `tests/annotation`). Значения по сплитам объединяются в `reports/annotation.json`. Ключи: `annotation.{agreement_iou, agreement_f1, n_checked}`; гейт G2: `agreement_iou` $\ge 0.80$.

## 10. Смещение геопривязки (G1)

Команда `geoqc data check-georef`, параметры `config/data.yaml → georef` (реализация на момент написания не сдана — сверить с `src/geoqc/data/README.md`, §13). Контрольные объекты — здания OSM площадью `min_area_m2`…`max_area_m2` (250…6000 м²), прямоугольностью $\ge$ `min_rectangularity` (0.70 — площадь / площадь минимального описанного прямоугольника), без соседей ближе `isolation_m` (12 м), не выше `max_levels` этажей (3, если тег есть); цель — `n_control_target` (40) объектов, минимум `n_control_min` (10). Для объекта $k$ в окне радиуса `search_radius_m` (10 м) ищется сдвиг $\delta_k \in \mathbb{R}^2$ с максимальным откликом корреляции между снимком и растеризованной маской OSM; отклики с отношением пик/среднее $<$ `min_peak_ratio` (1.6) отбрасываются.

$$
\mathrm{median\_offset\_m} = \operatorname{median}_k \|\delta_k\|_2,\qquad
(\Delta x,\Delta y) = \operatorname{median}_k \delta_k\ \text{(покомпонентно)}.
$$

Сдвиг применяется (`preprocess --shift`, `manifest.georef.shift_applied_m`), если медиана $>$ `apply_shift_over_m` (2.0 м — порог G1) **и** $\|(\Delta x,\Delta y)\|_2 \ge$ `min_systematic_shift_m` (0.6 м; иначе смещение не систематическое), после чего измерение повторяется. Пример: расстояния $1.2, 0.8, 3.1, 1.5, 0.9$ м → медиана $1.2$ м → G1 пройден, сдвиг не нужен. Ключи: `georeference.{median_offset_m, n_control_points}`; G1: $\le 2.0$ м после коррекции.

## 11. Векторизация: склейка, упрощение, min_area

Реализация — `src/geoqc/cv/vectorize.py`, параметры `config/cv.yaml → vectorize`.

- **Маска → полигоны** (`mask_to_geometry`): `rasterio.features.shapes` на бинарной маске тайла (пиксели nodata исключаются при `drop_nodata`), перенос из пикселей в CRS тайла через affine-transform, `make_valid`, `unary_union`. Площадь пикселя $= \mathrm{GSD}^2$: при целевом GSD 0.3 м (`config/data.yaml → preprocess.target_gsd_m`) — 0.09 м².
- **Склейка на перекрытии** (`merge_overlaps`): детекции *разных* тайлов объединяются транзитивно (union-find), если $\mathrm{IoU}(a,b) \ge$ `tile_overlap_merge_iou` (0.1) **или** $\dfrac{\operatorname{area}(a\cap b)}{\min(\operatorname{area}(a),\operatorname{area}(b))} \ge$ `min_overlap_ratio` (0.5); группа → `unary_union`; `score` $= \max_k s_k$ (`score_mode: max`) или $\sum_k s_k a_k / \sum_k a_k$ (`area_weighted`); `tile_id` — крупнейшего члена. Второе условие ловит фрагмент, отсечённый краем тайла: его IoU с целым зданием мал, а доля покрытия велика. Пример: здание $20\times10$ м разрезано границей тайлов на части $12\times10$ и $8\times10$ с общей полосой перекрытия $4\times10$: IoU $= 40/160 = 0.25 \ge 0.1$ → склейка.
- **Упрощение Дугласа–Пекера** (`simplify_detections`): `simplify(tolerance, preserve_topology=True)` с $\varepsilon$ = `simplify_tolerance_m` (0.5 м) в метрической CRS: вершина удаляется, если её расстояние до хорды между сохранёнными соседями $< \varepsilon$. Пиксельная «лестница» контура шагом 0.3 м исчезает, углы зданий сохраняются.
- **Фильтр площади** (`filter_min_area`): $\operatorname{area}(d) \ge$ `min_area_m2` (12 м², то же значение у разметки `annotation.min_area_m2`) — отбрасывает шум мельче ~$3.5\times3.5$ м. Не путать с `matching.min_area_m2` $=20$ — это нижняя граница нормировки площади в §6, а не фильтр.
- Результат → MultiPolygon → EPSG:4326 → `detections.geojson` / `buildings_det`.

## 12. Гейты G1–G9

Пороги — `config/quality_gates.yaml`; проверка `passed = actual <op> target`; отсутствующая метрика → `actual: null, passed: false, note`.

| Гейт | Метрика | Ключ `reports/metrics.json` | Условие | Определение |
|---|---|---|---|---|
| G1 | медианное смещение снимок/вектор после коррекции, м | `georeference.median_offset_m` | ≤ 2.0 | §10 |
| G2 | согласие разметки, IoU | `annotation.agreement_iou` | ≥ 0.80 | §9 |
| G3 | F1 детекции @ IoU ≥ 0.5, `val` | `detection.val.f1` | ≥ 0.70 | §3 |
| G4 | F1 детекции @ IoU ≥ 0.5, `test` | `detection.test.f1` | ≥ 0.70 | §3 |
| G5 | средний IoU сопоставленных, `test` | `detection.test.mean_iou_matched` | ≥ 0.60 | §4 |
| G6 | precision расхождений (ручная проверка) | `discrepancies.precision_overall` | ≥ 0.70 | §7 |
| G7 | precision@20 очереди | `discrepancies.precision_at_n.value` (`n = 20`) | ≥ 0.75 | §8 |
| G8 | воспроизводимость `make all` | `reproducibility.make_all_passed` | == true | флаг `eval gates --make-all-passed` |
| G9 | тесты | `tests.passed` (`tests.n_tests`) | == true | флаг `eval gates --tests-passed --n-tests` |

Результат каждого гейта: `gates.<G>.{target, op, actual, passed, note}`. Пороги не меняются после получения результатов на `test`; провал фиксируется с причиной и fallback.

Дополнительно: `comparison.detection_vs_secondary.{precision,recall,f1}` — P/R/F1 детекции и второго источника против ручной разметки `test` (одинаковая процедура §2–§3).

## 13. Статус синхронизации с кодом (2026-09-26)

Сверено с реализацией и конфигами: §5–§6 (`matching/scoring.py`, `matching/edge_cases.py`, `matching/README.md`, `config/matching.yaml`), §8 (`eval/discrepancy_metrics.py`, `config/eval.yaml`), §9 (`annotation/validate.py`, `config/annotation.yaml`), §11 (`cv/vectorize.py`, `config/cv.yaml`). После интеграции сверить:

- §10 — реализацию `geoqc data check-georef` (на момент написания не сдана) и `src/geoqc/data/README.md`;
- §2–§4 — соглашения о крайних случаях (пустые множества, деление на 0) в `src/geoqc/eval/README.md` и `eval/detection_metrics.py`;
- §12 — фактические значения появятся в `reports/metrics.json` после `geoqc eval gates`.
