SELECT count(*) FILTER (WHERE open IS NULL) AS n_open_null,
       count(*) AS n_total,
       count(*) FILTER (WHERE open IS NULL AND ts >= '2025-05-01') AS n_open_null_new,
       count(*) FILTER (WHERE ts >= '2025-05-01') AS n_new
FROM minute_bar;
