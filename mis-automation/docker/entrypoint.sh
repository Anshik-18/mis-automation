#!/bin/sh
set -e
# Apply migrations (idempotent, advisory-locked), seed PROPOSED profiles, then serve.
# Migration 002/003 are applied only if listed in MIS_APPROVED_MIGRATIONS (see README "Schema gaps").
python -m processor.cli migrate
python -m processor.cli seed
exec python -m processor.cli serve
