CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  model TEXT,
  started_at REAL NOT NULL,
  ended_at REAL
);
CREATE TABLE session_model_usage (
  session_id TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  actual_cost_usd REAL NOT NULL DEFAULT 0,
  first_seen REAL,
  last_seen REAL
);
INSERT INTO sessions (id, model, started_at, ended_at)
VALUES ('fixture-session', 'hermes-4', {{NOW}}, {{NOW}});
INSERT INTO session_model_usage (
  session_id, model, input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens, reasoning_tokens, actual_cost_usd, first_seen, last_seen
) VALUES ('fixture-session', 'hermes-4', 100, 40, 10, 5, 3, 1.25, {{NOW}}, {{NOW}});
