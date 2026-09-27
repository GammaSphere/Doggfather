-- 006_pairwise: optional pairwise judging ("which of these two is better?").

ALTER TABLE events ADD COLUMN pairwise_enabled INTEGER NOT NULL DEFAULT 0 CHECK (pairwise_enabled IN (0, 1));

CREATE TABLE pairwise_comparisons (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  winner_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  loser_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  CHECK (winner_id <> loser_id)
);
CREATE INDEX pairwise_event ON pairwise_comparisons(event_id);
-- A judge compares any given pair at most once, in either order.
CREATE UNIQUE INDEX pairwise_once ON pairwise_comparisons(judge_id, min(winner_id, loser_id), max(winner_id, loser_id));
