CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(32) PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum_sha256 CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT schema_migrations_checksum_format CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
);
