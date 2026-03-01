-- v0.28.0: Structured pricing + cache
CREATE TABLE IF NOT EXISTS pricing_discounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    group_name  TEXT NOT NULL,
    skill_group TEXT DEFAULT '*',
    effect_pct  REAL NOT NULL CHECK (effect_pct >= 0 AND effect_pct <= 100),
    priority    INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_cost_projection (
    skill_name    TEXT PRIMARY KEY,
    self_cost     REAL DEFAULT 0.0,
    ext_cost      REAL DEFAULT 0.0,
    total_cost    REAL DEFAULT 0.0,
    last_actual_self  REAL DEFAULT 0.0,
    last_actual_ext   REAL DEFAULT 0.0,
    updated_at    REAL NOT NULL
);

ALTER TABLE execution_log ADD COLUMN price_breakdown TEXT DEFAULT '';
