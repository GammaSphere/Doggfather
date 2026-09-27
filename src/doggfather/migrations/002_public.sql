-- 002_public: community voting (quadratic), and public comments.

ALTER TABLE events ADD COLUMN voting_mode TEXT NOT NULL DEFAULT 'off'
  CHECK (voting_mode IN ('off', 'link', 'email', 'account'));
ALTER TABLE events ADD COLUMN voting_open_at TEXT;
ALTER TABLE events ADD COLUMN voting_close_at TEXT;
ALTER TABLE events ADD COLUMN vote_credits INTEGER NOT NULL DEFAULT 25
  CHECK (vote_credits BETWEEN 1 AND 10000);

-- One row per identity that may hold a ballot. Which identity depends on the
-- event's voting mode: an account, a verified email address, or a device
-- token (link mode). Uniqueness per event is enforced for each kind.
CREATE TABLE voters (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL CHECK (kind IN ('account', 'email', 'link')),
  user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
  email       TEXT COLLATE NOCASE,  -- normalized: lower case, +tags stripped
  device_hash TEXT,                 -- keyed hash of the device cookie
  ip_hash     TEXT,                 -- keyed hash, never the raw address
  ua_hash     TEXT,
  created_at  TEXT NOT NULL,
  verified_at TEXT,
  flagged     TEXT,                 -- anomaly reason, for organizer review
  voided_at   TEXT,
  voided_by   TEXT REFERENCES users(id) ON DELETE SET NULL,
  UNIQUE (event_id, user_id),
  UNIQUE (event_id, email),
  UNIQUE (event_id, device_hash)
);
CREATE INDEX voters_ip ON voters(event_id, ip_hash);

CREATE TABLE ballot_items (
  voter_id   TEXT NOT NULL REFERENCES voters(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  votes      INTEGER NOT NULL CHECK (votes BETWEEN 1 AND 100),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (voter_id, project_id)
);
CREATE INDEX ballot_items_project ON ballot_items(project_id);

-- Quadratic voting: casting v votes costs v^2 credits. The service checks the
-- budget; these triggers make overspending impossible even for a buggy or
-- hostile code path.
CREATE TRIGGER ballot_budget_insert AFTER INSERT ON ballot_items
WHEN (SELECT SUM(votes * votes) FROM ballot_items WHERE voter_id = NEW.voter_id)
   > (SELECT e.vote_credits FROM voters v JOIN events e ON e.id = v.event_id WHERE v.id = NEW.voter_id)
BEGIN
  SELECT RAISE(ABORT, 'ballot exceeds the quadratic credit budget');
END;

CREATE TRIGGER ballot_budget_update AFTER UPDATE OF votes ON ballot_items
WHEN (SELECT SUM(votes * votes) FROM ballot_items WHERE voter_id = NEW.voter_id)
   > (SELECT e.vote_credits FROM voters v JOIN events e ON e.id = v.event_id WHERE v.id = NEW.voter_id)
BEGIN
  SELECT RAISE(ABORT, 'ballot exceeds the quadratic credit budget');
END;

-- One-time codes for email-gated voting (stored hashed, short-lived).
CREATE TABLE vote_codes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  email      TEXT NOT NULL COLLATE NOCASE,
  code_hash  TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  used_at    TEXT
);
CREATE INDEX vote_codes_lookup ON vote_codes(event_id, email);

CREATE TABLE comments (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body          TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  created_at    TEXT NOT NULL,
  hidden_at     TEXT,
  hidden_by     TEXT REFERENCES users(id) ON DELETE SET NULL,
  hidden_reason TEXT,
  ip_hash       TEXT
);
CREATE INDEX comments_project ON comments(project_id, created_at);
