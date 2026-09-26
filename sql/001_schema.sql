-- Data contract (docs/CONTRACTS.md §3). Owned by Orchestrator; changes only via DECISIONS.md.
-- Storage CRS: EPSG:4326. Metric computations (area, IoU) are done in the AOI UTM zone.
CREATE EXTENSION IF NOT EXISTS postgis;

-- Reference buildings: OSM (primary) and optional secondary sources.
CREATE TABLE IF NOT EXISTS buildings_ref (
    id            BIGSERIAL PRIMARY KEY,
    osm_id        BIGINT,
    geom          geometry(MultiPolygon, 4326) NOT NULL,
    tags          JSONB NOT NULL DEFAULT '{}'::jsonb,
    source        TEXT NOT NULL CHECK (source IN ('osm', 'microsoft', 'google')),
    snapshot_date DATE
);
CREATE INDEX IF NOT EXISTS buildings_ref_geom_gist ON buildings_ref USING GIST (geom);
CREATE INDEX IF NOT EXISTS buildings_ref_source_idx ON buildings_ref (source);

-- Detected buildings produced by the CV pipeline (after vectorization and tile merging).
CREATE TABLE IF NOT EXISTS buildings_det (
    id            BIGSERIAL PRIMARY KEY,
    geom          geometry(MultiPolygon, 4326) NOT NULL,
    score         DOUBLE PRECISION NOT NULL CHECK (score >= 0 AND score <= 1),
    model_version TEXT NOT NULL,
    tile_id       TEXT
);
CREATE INDEX IF NOT EXISTS buildings_det_geom_gist ON buildings_det USING GIST (geom);
CREATE INDEX IF NOT EXISTS buildings_det_model_idx ON buildings_det (model_version);

-- One-to-one matches ref <-> det with IoU >= iou_match (config/matching.yaml).
CREATE TABLE IF NOT EXISTS matches (
    id            BIGSERIAL PRIMARY KEY,
    ref_id        BIGINT NOT NULL REFERENCES buildings_ref(id) ON DELETE CASCADE,
    det_id        BIGINT NOT NULL REFERENCES buildings_det(id) ON DELETE CASCADE,
    iou           DOUBLE PRECISION NOT NULL,
    model_version TEXT NOT NULL,
    UNIQUE (ref_id, model_version),
    UNIQUE (det_id, model_version)
);

-- Review queue: the three discrepancy classes of the MVP.
CREATE TABLE IF NOT EXISTS discrepancies (
    id            BIGSERIAL PRIMARY KEY,
    class         TEXT NOT NULL CHECK (class IN ('missing_in_map', 'missing_on_image', 'geometry_mismatch')),
    ref_id        BIGINT REFERENCES buildings_ref(id) ON DELETE CASCADE,
    det_id        BIGINT REFERENCES buildings_det(id) ON DELETE CASCADE,
    iou           DOUBLE PRECISION,
    confidence    DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    geom          geometry(MultiPolygon, 4326) NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'confirmed', 'rejected')),
    model_version TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS discrepancies_geom_gist ON discrepancies USING GIST (geom);
CREATE INDEX IF NOT EXISTS discrepancies_class_conf_idx ON discrepancies (class, confidence DESC);
CREATE INDEX IF NOT EXISTS discrepancies_status_idx ON discrepancies (status);
