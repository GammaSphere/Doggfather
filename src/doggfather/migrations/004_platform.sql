-- 004_platform: personal API tokens and outgoing webhooks.

CREATE TABLE api_tokens (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  prefix       TEXT NOT NULL,          -- first characters, for recognising a token in the UI
  token_hash   TEXT NOT NULL UNIQUE,   -- SHA-256; the token itself is shown once
  created_at   TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at   TEXT
);
CREATE INDEX api_tokens_user ON api_tokens(user_id);

CREATE TABLE webhooks (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  url        TEXT NOT NULL,
  secret     TEXT NOT NULL,            -- HMAC key shared with the receiver
  topics     TEXT NOT NULL DEFAULT '*',
  active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
);

-- Transactional outbox: a delivery row is written in the same transaction
-- as the change it announces, then a background worker sends it with retries.
CREATE TABLE webhook_deliveries (
  id               TEXT PRIMARY KEY,
  webhook_id       TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  topic            TEXT NOT NULL,
  payload          TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at  TEXT NOT NULL,
  last_status_code INTEGER,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  delivered_at     TEXT
);
CREATE INDEX webhook_deliveries_due ON webhook_deliveries(status, next_attempt_at);
