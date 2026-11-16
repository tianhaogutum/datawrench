MODEL (name analytics.stage, kind VIEW);

SELECT id, ds FROM raw.input;
