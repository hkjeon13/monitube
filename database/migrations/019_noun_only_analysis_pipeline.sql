CREATE UNIQUE INDEX IF NOT EXISTS analysis_runs_target_v3_version_idx
  ON analysis_runs (target_id, data_version, pipeline_version)
  WHERE target_id IS NOT NULL AND pipeline_version = 'deterministic-v3';

CREATE UNIQUE INDEX IF NOT EXISTS analysis_runs_legacy_v3_version_idx
  ON analysis_runs (source_id, data_version, pipeline_version)
  WHERE target_id IS NULL AND source_id IS NOT NULL
    AND pipeline_version = 'deterministic-v3';
