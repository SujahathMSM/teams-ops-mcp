-- Migration 001: track when each token was last used.
-- Safe to run more than once. Apply to an existing Docker volume with:
--   docker compose exec db psql -U teamops -d teamops -f /migrations/001_users_last_used_at.sql

ALTER TABLE users ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
