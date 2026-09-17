-- 025_ask_elapsed.sql — DP phase 2e (Task 7): how long the answer took.
--
-- The first real page load took 185 seconds and nothing said so: the scope line
-- reported what was searched but not what it cost, and a tool that gets slower
-- in silence is the thing this project exists to prevent ("不让工具静默变慢").
-- Every ask now records its wall time, and the scope line prints it.
ALTER TABLE ask_log ADD COLUMN elapsed_ms INTEGER;
