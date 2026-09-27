-- 007_hardening: a per-track weight must pair a criterion and a track from
-- the same event. Without this, an organizer of one event could re-weight
-- another event's rubric through crafted override keys (security review).

CREATE TRIGGER criterion_track_weights_same_event_insert
BEFORE INSERT ON criterion_track_weights
WHEN (SELECT event_id FROM criteria WHERE id = NEW.criterion_id)
  IS NOT (SELECT event_id FROM tracks WHERE id = NEW.track_id)
BEGIN
  SELECT RAISE(ABORT, 'criterion and track must belong to the same event');
END;

CREATE TRIGGER criterion_track_weights_same_event_update
BEFORE UPDATE ON criterion_track_weights
WHEN (SELECT event_id FROM criteria WHERE id = NEW.criterion_id)
  IS NOT (SELECT event_id FROM tracks WHERE id = NEW.track_id)
BEGIN
  SELECT RAISE(ABORT, 'criterion and track must belong to the same event');
END;
