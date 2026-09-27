-- 005_records: prize awards and signed, publicly verifiable records.

CREATE TABLE prize_awards (
  prize_id   TEXT NOT NULL REFERENCES prizes(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  awarded_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  awarded_at TEXT NOT NULL,
  PRIMARY KEY (prize_id, project_id)
);

-- Each record stores the exact canonical JSON that was signed, so a
-- verifier needs nothing but this row (or its export) and the public key.
CREATE TABLE records (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL CHECK (kind IN ('judge_record', 'participation', 'award')),
  event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  subject_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
  subject_name    TEXT NOT NULL,
  subject_key     TEXT NOT NULL,   -- kind:event:user[:prize], one live record each
  payload         TEXT NOT NULL,
  signature       TEXT NOT NULL,   -- Ed25519, base64url
  key_id          TEXT NOT NULL,
  issued_at       TEXT NOT NULL,
  revoked_at      TEXT,
  revoked_reason  TEXT
);
CREATE UNIQUE INDEX records_live_subject ON records(subject_key) WHERE revoked_at IS NULL;
CREATE INDEX records_event ON records(event_id, kind);
CREATE INDEX records_subject ON records(subject_user_id);

-- A signed record never changes; revocation is the only allowed update.
CREATE TRIGGER records_immutable BEFORE UPDATE OF payload, signature, key_id, subject_key ON records
BEGIN
  SELECT RAISE(ABORT, 'signed records are immutable; revoke and reissue instead');
END;
