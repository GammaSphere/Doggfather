-- 001_init: identity, events, teams, submissions, judging, audit.
--
-- Conventions
--   * ids are opaque TEXT. Imported ids (evt_01, prj_07, jdg_26) are kept
--     verbatim; new rows get prefixed random ids (prj_3f9a1c2b7e).
--   * timestamps are TEXT in canonical ISO 8601 UTC (2026-03-01T18:00:00Z),
--     so lexical order is chronological order and CHECKs can compare them.
--   * invariants that must hold no matter which code path writes live here,
--     as constraints and triggers, not only in the service layer.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------- identity

CREATE TABLE users (
  id                TEXT PRIMARY KEY,
  email             TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name              TEXT NOT NULL,
  -- NULL for imported or invited people who have never set a password.
  password_hash     TEXT,
  is_admin          INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
  email_verified_at TEXT,
  created_at        TEXT NOT NULL
);

CREATE TABLE sessions (
  -- SHA-256 of the cookie value. A database leak does not leak sessions.
  token_hash   TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);

CREATE TABLE password_resets (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at    TEXT
);

-- ------------------------------------------------------------------ events

CREATE TABLE events (
  id                   TEXT PRIMARY KEY,
  slug                 TEXT NOT NULL UNIQUE,
  name                 TEXT NOT NULL,
  tagline              TEXT NOT NULL DEFAULT '',
  description          TEXT NOT NULL DEFAULT '',
  submissions_open_at  TEXT NOT NULL,
  submissions_close_at TEXT NOT NULL,
  judging_close_at     TEXT NOT NULL,
  max_team_size        INTEGER NOT NULL DEFAULT 4 CHECK (max_team_size BETWEEN 1 AND 50),
  review_target        INTEGER NOT NULL DEFAULT 3 CHECK (review_target BETWEEN 1 AND 20),
  normalization        TEXT NOT NULL DEFAULT 'bias'
                       CHECK (normalization IN ('raw', 'zscore', 'bias', 'pairwise')),
  results_published_at TEXT,
  created_by           TEXT REFERENCES users(id) ON DELETE SET NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  CHECK (submissions_open_at < submissions_close_at),
  CHECK (submissions_close_at <= judging_close_at)
);

-- Roles are scoped to an event: someone can judge one event and compete in
-- the next. Admin is global and lives on users. Visitor is "no session".
CREATE TABLE event_members (
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('organizer', 'judge', 'participant')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (event_id, user_id, role)
);
CREATE INDEX event_members_user ON event_members(user_id, role);

-- A judge cannot also compete in the same event (conflict of interest).
CREATE TRIGGER event_members_no_judge_participant
BEFORE INSERT ON event_members
WHEN (NEW.role = 'judge' AND EXISTS (
        SELECT 1 FROM event_members
        WHERE event_id = NEW.event_id AND user_id = NEW.user_id AND role = 'participant'))
  OR (NEW.role = 'participant' AND EXISTS (
        SELECT 1 FROM event_members
        WHERE event_id = NEW.event_id AND user_id = NEW.user_id AND role = 'judge'))
BEGIN
  SELECT RAISE(ABORT, 'conflict of interest: judge and participant in the same event');
END;

CREATE TABLE tracks (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  position    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (event_id, name),
  UNIQUE (id, event_id)
);

CREATE TABLE prizes (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  track_id    TEXT,               -- NULL means an overall prize
  name        TEXT NOT NULL,
  value       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  position    INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (track_id, event_id) REFERENCES tracks(id, event_id) ON DELETE CASCADE
);

CREATE TABLE custom_questions (
  id       TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  prompt   TEXT NOT NULL,
  help     TEXT NOT NULL DEFAULT '',
  kind     TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'textarea', 'url', 'choice')),
  options  TEXT NOT NULL DEFAULT '[]',   -- JSON array, used by kind = 'choice'
  required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0, 1)),
  position INTEGER NOT NULL DEFAULT 0
);

-- Which tracks a judge may see. Isolation queries join through this table.
CREATE TABLE judge_tracks (
  event_id TEXT NOT NULL,
  user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  track_id TEXT NOT NULL,
  PRIMARY KEY (user_id, track_id),
  FOREIGN KEY (track_id, event_id) REFERENCES tracks(id, event_id) ON DELETE CASCADE
);
CREATE INDEX judge_tracks_event ON judge_tracks(event_id, user_id);

-- ------------------------------------------------------------------- teams

CREATE TABLE teams (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  -- Deliberately not unique: the fixtures contain three teams called
  -- "StillTrail". People are allowed to pick the same name.
  name        TEXT NOT NULL,
  invite_code TEXT NOT NULL UNIQUE,
  created_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL,
  UNIQUE (id, event_id)
);

CREATE TABLE team_members (
  team_id   TEXT NOT NULL,
  event_id  TEXT NOT NULL,
  user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role      TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('captain', 'member')),
  joined_at TEXT NOT NULL,
  PRIMARY KEY (team_id, user_id),
  -- One team per person per event, enforced by the database.
  UNIQUE (event_id, user_id),
  FOREIGN KEY (team_id, event_id) REFERENCES teams(id, event_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------- projects

CREATE TABLE projects (
  id               TEXT PRIMARY KEY,
  event_id         TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  team_id          TEXT NOT NULL,
  track_id         TEXT,
  title            TEXT NOT NULL,
  tagline          TEXT NOT NULL DEFAULT '',
  description      TEXT NOT NULL DEFAULT '',
  thumbnail        TEXT,                        -- stored upload file name
  video_url        TEXT NOT NULL DEFAULT '',
  repo_url         TEXT NOT NULL DEFAULT '',
  demo_url         TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'withdrawn')),
  submitted_at     TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  duplicate_of     TEXT REFERENCES projects(id) ON DELETE SET NULL,
  duplicate_reason TEXT,
  CHECK (status <> 'submitted' OR submitted_at IS NOT NULL),
  FOREIGN KEY (team_id, event_id) REFERENCES teams(id, event_id) ON DELETE CASCADE,
  -- A project's track must belong to the project's event. NO ACTION (checked
  -- at statement end) so deleting a whole event can still cascade.
  FOREIGN KEY (track_id, event_id) REFERENCES tracks(id, event_id)
);
CREATE INDEX projects_event_status ON projects(event_id, status);
CREATE INDEX projects_team ON projects(team_id);

CREATE TABLE project_tags (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  tag        TEXT NOT NULL,
  PRIMARY KEY (project_id, tag)
);
CREATE INDEX project_tags_tag ON project_tags(tag);

CREATE TABLE project_images (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  file       TEXT NOT NULL,
  caption    TEXT NOT NULL DEFAULT '',
  position   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE project_answers (
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  question_id TEXT NOT NULL REFERENCES custom_questions(id) ON DELETE CASCADE,
  answer      TEXT NOT NULL,
  PRIMARY KEY (project_id, question_id)
);

-- ----------------------------------------------------------------- judging

CREATE TABLE criteria (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  key         TEXT NOT NULL,
  label       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  weight      REAL NOT NULL DEFAULT 1 CHECK (weight >= 0),
  min_score   INTEGER NOT NULL DEFAULT 1,
  max_score   INTEGER NOT NULL DEFAULT 5,
  position    INTEGER NOT NULL DEFAULT 0,
  CHECK (min_score < max_score),
  UNIQUE (event_id, key)
);

-- Optional per-track weight overrides ("security projects weigh quality more").
CREATE TABLE criterion_track_weights (
  criterion_id TEXT NOT NULL REFERENCES criteria(id) ON DELETE CASCADE,
  track_id     TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  weight       REAL NOT NULL CHECK (weight >= 0),
  PRIMARY KEY (criterion_id, track_id)
);

CREATE TABLE assignments (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  batch       TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
  assigned_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  assigned_at TEXT NOT NULL,
  UNIQUE (judge_id, project_id)
);
CREATE INDEX assignments_event ON assignments(event_id, status);
CREATE INDEX assignments_project ON assignments(project_id);

CREATE TABLE scores (
  id            TEXT PRIMARY KEY,
  assignment_id TEXT NOT NULL UNIQUE REFERENCES assignments(id) ON DELETE CASCADE,
  event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  comment       TEXT NOT NULL DEFAULT '',
  submitted_at  TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (judge_id, project_id)
);
CREATE INDEX scores_event ON scores(event_id);

CREATE TABLE score_items (
  score_id     TEXT NOT NULL REFERENCES scores(id) ON DELETE CASCADE,
  criterion_id TEXT NOT NULL REFERENCES criteria(id) ON DELETE CASCADE,
  value        INTEGER NOT NULL,
  PRIMARY KEY (score_id, criterion_id)
);

-- A score outside the rubric's range is rejected by the database itself.
CREATE TRIGGER score_items_range_insert
BEFORE INSERT ON score_items
WHEN NEW.value < (SELECT min_score FROM criteria WHERE id = NEW.criterion_id)
  OR NEW.value > (SELECT max_score FROM criteria WHERE id = NEW.criterion_id)
BEGIN
  SELECT RAISE(ABORT, 'score outside the criterion range');
END;

CREATE TRIGGER score_items_range_update
BEFORE UPDATE OF value ON score_items
WHEN NEW.value < (SELECT min_score FROM criteria WHERE id = NEW.criterion_id)
  OR NEW.value > (SELECT max_score FROM criteria WHERE id = NEW.criterion_id)
BEGIN
  SELECT RAISE(ABORT, 'score outside the criterion range');
END;

-- ----------------------------------------------------------- invites, mail

CREATE TABLE invites (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  email       TEXT NOT NULL COLLATE NOCASE,
  role        TEXT NOT NULL CHECK (role IN ('judge', 'organizer')),
  track_ids   TEXT NOT NULL DEFAULT '[]',
  token_hash  TEXT NOT NULL UNIQUE,
  invited_by  TEXT REFERENCES users(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  accepted_at TEXT,
  accepted_by TEXT REFERENCES users(id) ON DELETE SET NULL
);

-- Offline-first mail: every message lands here. If SMTP is configured it is
-- also delivered; either way admins can read what was sent.
CREATE TABLE outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  to_email   TEXT NOT NULL,
  subject    TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at    TEXT,
  error      TEXT
);

-- ------------------------------------------------------------------- audit

-- Append-only and hash-chained: each row's hash covers its content and the
-- previous row's hash, so editing or deleting history is detectable.
CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  actor_id    TEXT,
  actor_label TEXT NOT NULL,
  action      TEXT NOT NULL,
  event_id    TEXT,
  target_type TEXT,
  target_id   TEXT,
  detail      TEXT NOT NULL DEFAULT '{}',
  ip          TEXT,
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL UNIQUE
);
CREATE INDEX audit_log_event ON audit_log(event_id, id);

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit log is append-only');
END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit log is append-only');
END;
