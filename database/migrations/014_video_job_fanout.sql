-- Split channel and keyword discovery from per-video collection work. Parent
-- jobs retain target/request ownership; child jobs have no target_id so the
-- existing one-active-job-per-target guarantee remains intact.
ALTER TABLE sync_jobs
  ADD COLUMN IF NOT EXISTS parent_job_id UUID REFERENCES sync_jobs(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS sync_jobs_parent_job_state_idx
  ON sync_jobs (parent_job_id, state, created_at)
  WHERE parent_job_id IS NOT NULL;
