-- Apply once through reviewed schema rollout; no application transaction changes.
-- Existing payment/adverse indexes begin with provider, not campaign event ID.
-- Covers the entire read; monitoring never falls back to a table/body scan.
CREATE INDEX tr_trust_event_by_reward_event
ON tr_trust_event (event_id, workspace_id)
STORING (kind, credited_micro, recorded_at);
