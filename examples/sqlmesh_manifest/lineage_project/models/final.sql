MODEL (
  name analytics.final,
  kind INCREMENTAL_BY_TIME_RANGE (time_column ds),
  cron '@hourly',
  audits (not_null(columns := (id)))
);

-- A workflow statement, preserved in raw_code but never executed or mapped to a dbt hook.
CREATE TABLE IF NOT EXISTS hook_example (id INT);

SELECT id, ds FROM analytics.stage;
