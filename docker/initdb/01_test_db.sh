#!/bin/bash
# Runs once on first container start: creates the test database with PostGIS.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS postgis;"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE geoqc_test;"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d geoqc_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
