-- 003_integrity: organizers can dismiss a duplicate flag, and the dismissal
-- sticks when detection runs again.

ALTER TABLE projects ADD COLUMN duplicate_dismissed_at TEXT;
ALTER TABLE projects ADD COLUMN withdrawn_reason TEXT;
