-- Matching layer (Geo Agent). Idempotent: safe to re-apply (CREATE OR REPLACE / IF NOT EXISTS).
-- Storage CRS is EPSG:4326; every area / IoU is computed after ST_Transform into the AOI UTM zone
-- (metric_srid, config/pipeline.yaml -> crs.metric). Only buildings_ref.source = 'osm' is the
-- reference layer; 'microsoft' / 'google' rows are a secondary source used for scoring only.

-- ---------------------------------------------------------------------------------------------
-- Auxiliary tables (not part of the 001 contract; owned by the matching module)
-- ---------------------------------------------------------------------------------------------

-- Detections suppressed by pre-matching de-duplication (NMS with IoU > dedup_iou).
-- Rows are never deleted from buildings_det; suppressed ids are excluded from matching.
CREATE TABLE IF NOT EXISTS det_suppressed (
    det_id        BIGINT PRIMARY KEY REFERENCES buildings_det(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    kept_det_id   BIGINT NOT NULL REFERENCES buildings_det(id) ON DELETE CASCADE,
    iou           DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS det_suppressed_model_idx ON det_suppressed (model_version);

-- Scoring features behind discrepancies.confidence (for error analysis and reproducibility).
CREATE TABLE IF NOT EXISTS discrepancy_features (
    discrepancy_id     BIGINT PRIMARY KEY REFERENCES discrepancies(id) ON DELETE CASCADE,
    score              DOUBLE PRECISION,
    area_m2            DOUBLE PRECISION,
    area_norm          DOUBLE PRECISION,
    secondary_coverage DOUBLE PRECISION,
    has_secondary      BOOLEAN NOT NULL DEFAULT false,
    max_iou_any        DOUBLE PRECISION,
    aoi_edge           BOOLEAN NOT NULL DEFAULT false,
    confidence_raw     DOUBLE PRECISION,
    confidence         DOUBLE PRECISION
);

-- ---------------------------------------------------------------------------------------------
-- IoU
-- ---------------------------------------------------------------------------------------------

-- IoU(A, B) = area(A ∩ B) / area(A ∪ B), with area(A ∪ B) = area(A) + area(B) - area(A ∩ B).
-- Returns 0 for disjoint or degenerate input. Geometries must be valid.
CREATE OR REPLACE FUNCTION geoqc_iou(a geometry, b geometry, metric_srid integer)
RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE
        WHEN NOT ST_Intersects(a, b) THEN 0.0
        ELSE (
            SELECT CASE WHEN u <= 0 THEN 0.0 ELSE i / u END
            FROM (
                SELECT i, ST_Area(am) + ST_Area(bm) - i AS u
                FROM (SELECT ST_Transform(a, metric_srid) AS am, ST_Transform(b, metric_srid) AS bm) t,
                     LATERAL (SELECT ST_Area(ST_Intersection(am, bm)) AS i) x
            ) s
        )
    END
$$;

-- ---------------------------------------------------------------------------------------------
-- Candidate pairs (GiST) and greedy 1:1 matching
-- ---------------------------------------------------------------------------------------------

-- Every (osm ref, det) pair that intersects (GiST on both geom columns) with IoU >= p_min_iou.
-- Suppressed detections (det_suppressed) are excluded.
CREATE OR REPLACE FUNCTION geoqc_candidate_pairs(
    p_model_version text,
    p_metric_srid   integer,
    p_min_iou       double precision DEFAULT 0.0
)
RETURNS TABLE(ref_id bigint, det_id bigint, iou double precision)
LANGUAGE sql STABLE AS $$
    SELECT c.ref_id, c.det_id, c.iou
    FROM (
        SELECT r.id AS ref_id, d.id AS det_id, geoqc_iou(r.geom, d.geom, p_metric_srid) AS iou
        FROM buildings_ref r
        JOIN buildings_det d
          ON d.model_version = p_model_version
         AND ST_Intersects(r.geom, d.geom)
        WHERE r.source = 'osm'
          AND NOT EXISTS (SELECT 1 FROM det_suppressed s WHERE s.det_id = d.id)
    ) c
    WHERE c.iou >= p_min_iou
$$;

-- Greedy 1:1 assignment: candidates with IoU >= p_iou_match are visited by IoU DESC
-- (ties: ref_id, det_id); a pair is accepted when neither side is taken yet. The two UNIQUE
-- constraints of `matches` enforce 1:1, so the loop is a plain INSERT ... ON CONFLICT DO NOTHING.
-- Idempotent per model_version (DELETE before INSERT). Returns the number of matches written.
CREATE OR REPLACE FUNCTION geoqc_match_greedy(
    p_model_version text,
    p_metric_srid   integer,
    p_iou_match     double precision
)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    r        record;
    n        integer := 0;
    inserted integer;
BEGIN
    DELETE FROM matches WHERE model_version = p_model_version;
    FOR r IN
        SELECT c.ref_id, c.det_id, c.iou
        FROM geoqc_candidate_pairs(p_model_version, p_metric_srid, p_iou_match) c
        ORDER BY c.iou DESC, c.ref_id, c.det_id
    LOOP
        INSERT INTO matches (ref_id, det_id, iou, model_version)
        VALUES (r.ref_id, r.det_id, r.iou, p_model_version)
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS inserted = ROW_COUNT;
        n := n + inserted;
    END LOOP;
    RETURN n;
END
$$;

-- ---------------------------------------------------------------------------------------------
-- Classification into discrepancies
-- ---------------------------------------------------------------------------------------------

-- Uses the current content of `matches` for p_model_version (greedy or Hungarian) and writes
-- the three MVP classes. Idempotent per model_version (DELETE before INSERT).
--   geometry_mismatch: unmatched det whose best unmatched osm ref has IoU >= p_iou_mismatch_min
--                      (with a maximal 1:1 assignment such a pair always has IoU < p_iou_match);
--                      geom = union(ref, det), iou = pair IoU.
--   missing_in_map:    every other unmatched (and not suppressed) det; iou = max IoU with any
--                      osm ref (NULL when nothing intersects); geom = det.
--   missing_on_image:  osm ref that is neither matched nor the partner of a geometry_mismatch;
--                      iou = max IoU with any det of the model version; geom = ref.
-- When p_aoi_geojson is given, refs that do not intersect the AOI are ignored (no imagery there).
-- `confidence` is provisional (score / 1 - iou / 0.5); geoqc matching run replaces it with the
-- weighted formula from src/geoqc/matching/scoring.py. Returns counts per class.
CREATE OR REPLACE FUNCTION geoqc_classify(
    p_model_version    text,
    p_metric_srid      integer,
    p_iou_match        double precision,
    p_iou_mismatch_min double precision,
    p_aoi_geojson      text DEFAULT NULL
)
RETURNS TABLE(class text, n bigint)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    aoi geometry := CASE WHEN p_aoi_geojson IS NULL THEN NULL
                         ELSE ST_SetSRID(ST_GeomFromGeoJSON(p_aoi_geojson), 4326) END;
BEGIN
    DELETE FROM discrepancies WHERE model_version = p_model_version;

    CREATE TEMP TABLE IF NOT EXISTS _geoqc_cand (
        ref_id bigint, det_id bigint, iou double precision
    ) ON COMMIT DROP;
    TRUNCATE _geoqc_cand;
    INSERT INTO _geoqc_cand
        SELECT c.ref_id, c.det_id, c.iou
        FROM geoqc_candidate_pairs(p_model_version, p_metric_srid, 0.0) c;

    -- geometry_mismatch: best unmatched ref for each unmatched det
    INSERT INTO discrepancies (class, ref_id, det_id, iou, confidence, geom, model_version)
    SELECT 'geometry_mismatch', b.ref_id, b.det_id, b.iou,
           LEAST(1.0, GREATEST(0.0, 1.0 - b.iou)),
           ST_Multi(ST_CollectionExtract(ST_Union(r.geom, d.geom), 3)),
           p_model_version
    FROM (
        SELECT DISTINCT ON (c.det_id) c.det_id, c.ref_id, c.iou
        FROM _geoqc_cand c
        WHERE NOT EXISTS (SELECT 1 FROM matches m
                          WHERE m.model_version = p_model_version AND m.det_id = c.det_id)
          AND NOT EXISTS (SELECT 1 FROM matches m
                          WHERE m.model_version = p_model_version AND m.ref_id = c.ref_id)
        ORDER BY c.det_id, c.iou DESC, c.ref_id
    ) b
    JOIN buildings_ref r ON r.id = b.ref_id
    JOIN buildings_det d ON d.id = b.det_id
    WHERE b.iou >= p_iou_mismatch_min;

    -- missing_in_map: remaining unmatched, non-suppressed detections
    INSERT INTO discrepancies (class, ref_id, det_id, iou, confidence, geom, model_version)
    SELECT 'missing_in_map', NULL, d.id,
           (SELECT max(c.iou) FROM _geoqc_cand c WHERE c.det_id = d.id),
           d.score, d.geom, p_model_version
    FROM buildings_det d
    WHERE d.model_version = p_model_version
      AND NOT EXISTS (SELECT 1 FROM det_suppressed s WHERE s.det_id = d.id)
      AND NOT EXISTS (SELECT 1 FROM matches m
                      WHERE m.model_version = p_model_version AND m.det_id = d.id)
      AND NOT EXISTS (SELECT 1 FROM discrepancies x
                      WHERE x.model_version = p_model_version AND x.det_id = d.id);

    -- missing_on_image: osm refs without any partner
    INSERT INTO discrepancies (class, ref_id, det_id, iou, confidence, geom, model_version)
    SELECT 'missing_on_image', r.id, NULL,
           (SELECT max(c.iou) FROM _geoqc_cand c WHERE c.ref_id = r.id),
           0.5, r.geom, p_model_version
    FROM buildings_ref r
    WHERE r.source = 'osm'
      AND (aoi IS NULL OR ST_Intersects(r.geom, aoi))
      AND NOT EXISTS (SELECT 1 FROM matches m
                      WHERE m.model_version = p_model_version AND m.ref_id = r.id)
      AND NOT EXISTS (SELECT 1 FROM discrepancies x
                      WHERE x.model_version = p_model_version AND x.ref_id = r.id);

    RETURN QUERY
        SELECT x.class, count(*)::bigint
        FROM discrepancies x
        WHERE x.model_version = p_model_version
        GROUP BY x.class
        ORDER BY x.class;
END
$$;

-- ---------------------------------------------------------------------------------------------
-- Edge cases: de-duplication (NMS) and multipart detections
-- ---------------------------------------------------------------------------------------------

-- Non-maximum suppression among detections of one model version: visit detections by score
-- DESC (ties: id); a kept detection suppresses every not-yet-kept neighbour with IoU > p_dedup_iou.
-- Writes det_suppressed (idempotent per model_version). Returns the number suppressed.
CREATE OR REPLACE FUNCTION geoqc_dedup_detections(
    p_model_version text,
    p_metric_srid   integer,
    p_dedup_iou     double precision
)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    n integer := 0;
    k integer;
BEGIN
    DELETE FROM det_suppressed WHERE model_version = p_model_version;

    DROP TABLE IF EXISTS _geoqc_dup_pairs;
    CREATE TEMP TABLE _geoqc_dup_pairs ON COMMIT DROP AS
        WITH p AS (
            SELECT a.id AS a_id, b.id AS b_id, geoqc_iou(a.geom, b.geom, p_metric_srid) AS iou
            FROM buildings_det a
            JOIN buildings_det b
              ON b.model_version = a.model_version
             AND a.id < b.id
             AND ST_Intersects(a.geom, b.geom)
            WHERE a.model_version = p_model_version
        )
        SELECT a_id, b_id, iou FROM p WHERE iou > p_dedup_iou
        UNION ALL
        SELECT b_id, a_id, iou FROM p WHERE iou > p_dedup_iou;
    CREATE INDEX ON _geoqc_dup_pairs (a_id);

    DROP TABLE IF EXISTS _geoqc_kept;
    CREATE TEMP TABLE _geoqc_kept (id bigint PRIMARY KEY) ON COMMIT DROP;

    FOR r IN
        SELECT d.id
        FROM buildings_det d
        WHERE d.model_version = p_model_version
          AND EXISTS (SELECT 1 FROM _geoqc_dup_pairs p WHERE p.a_id = d.id)
        ORDER BY d.score DESC, d.id
    LOOP
        IF EXISTS (SELECT 1 FROM det_suppressed s WHERE s.det_id = r.id) THEN
            CONTINUE;
        END IF;
        INSERT INTO _geoqc_kept VALUES (r.id);
        INSERT INTO det_suppressed (det_id, model_version, kept_det_id, iou)
        SELECT p.b_id, p_model_version, r.id, p.iou
        FROM _geoqc_dup_pairs p
        WHERE p.a_id = r.id
          AND NOT EXISTS (SELECT 1 FROM _geoqc_kept k WHERE k.id = p.b_id)
        ON CONFLICT (det_id) DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT;
        n := n + k;
    END LOOP;
    RETURN n;
END
$$;

-- Split multipart detections into one row per part (score / model_version / tile_id copied).
-- Idempotent: after one call no detection of the model version has more than one part.
-- Returns the number of original multipart rows that were split.
CREATE OR REPLACE FUNCTION geoqc_split_multipart_detections(p_model_version text)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    n integer;
BEGIN
    WITH del AS (
        DELETE FROM buildings_det
        WHERE model_version = p_model_version AND ST_NumGeometries(geom) > 1
        RETURNING id, geom, score, model_version, tile_id
    ), ins AS (
        INSERT INTO buildings_det (geom, score, model_version, tile_id)
        SELECT ST_Multi((dp).geom), del.score, del.model_version, del.tile_id
        FROM del, LATERAL ST_Dump(del.geom) dp
        RETURNING 1
    )
    SELECT count(*) INTO n FROM del;
    RETURN n;
END
$$;

-- ---------------------------------------------------------------------------------------------
-- Scoring features
-- ---------------------------------------------------------------------------------------------

-- Per-discrepancy features consumed by scoring.py. The "subject" geometry is the det for
-- missing_in_map / geometry_mismatch and the ref for missing_on_image.
--   area_m2            area of the subject in the metric CRS
--   secondary_coverage area(subject ∩ ∪ secondary footprints) / area(subject)  (0 when none)
--   aoi_edge           subject is not covered by AOI shrunk by p_aoi_edge_buffer_m (false w/o AOI)
CREATE OR REPLACE FUNCTION geoqc_discrepancy_features(
    p_model_version     text,
    p_metric_srid       integer,
    p_aoi_geojson       text DEFAULT NULL,
    p_aoi_edge_buffer_m double precision DEFAULT 0.0
)
RETURNS TABLE(
    id bigint, class text, ref_id bigint, det_id bigint, iou double precision,
    score double precision, area_m2 double precision, secondary_coverage double precision,
    aoi_edge boolean
)
LANGUAGE sql STABLE AS $$
    WITH aoi AS (
        SELECT CASE
            WHEN p_aoi_geojson IS NULL THEN NULL::geometry
            ELSE ST_Transform(
                     ST_Buffer(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(p_aoi_geojson), 4326),
                                            p_metric_srid),
                               -p_aoi_edge_buffer_m),
                     4326)
        END AS inner_geom
    ),
    subj AS (
        SELECT x.id, x.class, x.ref_id, x.det_id, x.iou, d.score,
               CASE WHEN x.class = 'missing_on_image' THEN r.geom ELSE d.geom END AS geom
        FROM discrepancies x
        LEFT JOIN buildings_det d ON d.id = x.det_id
        LEFT JOIN buildings_ref r ON r.id = x.ref_id
        WHERE x.model_version = p_model_version
    )
    SELECT s.id, s.class, s.ref_id, s.det_id, s.iou, s.score,
           ST_Area(ST_Transform(s.geom, p_metric_srid)) AS area_m2,
           COALESCE(sec.cov, 0.0) AS secondary_coverage,
           CASE WHEN a.inner_geom IS NULL THEN false
                ELSE NOT ST_Covers(a.inner_geom, s.geom) END AS aoi_edge
    FROM subj s
    CROSS JOIN aoi a
    LEFT JOIN LATERAL (
        SELECT ST_Area(ST_Transform(ST_Intersection(s.geom, ST_Union(b.geom)), p_metric_srid))
               / NULLIF(ST_Area(ST_Transform(s.geom, p_metric_srid)), 0) AS cov
        FROM buildings_ref b
        WHERE b.source IN ('microsoft', 'google') AND ST_Intersects(b.geom, s.geom)
    ) sec ON true
$$;
