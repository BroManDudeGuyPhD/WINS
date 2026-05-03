-- WINS PostgreSQL schema
-- Run automatically on first container start via docker-entrypoint-initdb.d

CREATE TABLE IF NOT EXISTS signal_log (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    token           VARCHAR(20) NOT NULL,
    signal_type     VARCHAR(30) NOT NULL,    -- catalyst | sentiment | momentum | macro
    raw_data        JSONB,
    summary         TEXT
);

CREATE TABLE IF NOT EXISTS decision_log (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    token           VARCHAR(20) NOT NULL,
    action          VARCHAR(10) NOT NULL,    -- buy | sell | hold
    confidence      NUMERIC(4,3),
    signal_type     VARCHAR(30),
    entry_price     NUMERIC(20,8),
    stop_loss_price NUMERIC(20,8),
    target_price    NUMERIC(20,8),
    estimated_move_pct NUMERIC(6,2),
    time_horizon    VARCHAR(20),
    reasoning       TEXT,
    macro_gate      VARCHAR(10),
    risk_flag       VARCHAR(10),
    raw_response    JSONB,
    model_used        VARCHAR(50),
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cache_read_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS trade_log (
    id              SERIAL PRIMARY KEY,
    ts_open         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ts_close        TIMESTAMPTZ,
    decision_id     INTEGER REFERENCES decision_log(id),
    token           VARCHAR(20) NOT NULL,
    trade_mode      VARCHAR(10) NOT NULL,   -- paper | live
    side            VARCHAR(5) NOT NULL,    -- buy | sell
    qty             NUMERIC(20,8),
    entry_price     NUMERIC(20,8),
    exit_price      NUMERIC(20,8),
    stop_loss_price NUMERIC(20,8),
    target_price    NUMERIC(20,8),
    pnl_usd         NUMERIC(12,4),
    pnl_pct         NUMERIC(8,4),
    exit_reason     VARCHAR(30),            -- stop_loss | target | manual_exit | system_pause
    exchange_order_id VARCHAR(100),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS system_state (
    id                    SERIAL PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    run_number            INTEGER NOT NULL DEFAULT 1,
    phase                 VARCHAR(20) NOT NULL DEFAULT 'paper',   -- paper | run1 | run2 | run3 | scale250 | scale500 | scale1000
    capital_usd           NUMERIC(12,4) NOT NULL,
    run_starting_capital  NUMERIC(12,4),                          -- capital at run start; drawdown kill switch baseline
    trade_mode            VARCHAR(10) NOT NULL DEFAULT 'paper',
    system_paused         BOOLEAN NOT NULL DEFAULT FALSE,
    pause_reason          TEXT,
    open_positions        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS macro_log (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    btc_price       NUMERIC(20,8),
    btc_dominance   NUMERIC(6,3),
    btc_24h_change  NUMERIC(6,3),
    macro_verdict   VARCHAR(10) NOT NULL,   -- risk_on | risk_off | neutral
    reasoning       TEXT
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_decision_log_ts    ON decision_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_trade_log_ts_open  ON trade_log (ts_open DESC);
CREATE INDEX IF NOT EXISTS idx_signal_log_ts      ON signal_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_macro_log_ts       ON macro_log (ts DESC);

CREATE TABLE IF NOT EXISTS calibration_result (
    id           SERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bucket       VARCHAR(20) NOT NULL,   -- low (0.65-0.75) | mid (0.75-0.85) | high (0.85+)
    trade_count  INTEGER NOT NULL,
    win_count    INTEGER NOT NULL,
    win_rate     NUMERIC(5,4) NOT NULL,
    multiplier   NUMERIC(5,4) NOT NULL,
    enforced     BOOLEAN NOT NULL DEFAULT FALSE  -- false until trade_count >= 30
);

CREATE INDEX IF NOT EXISTS idx_calibration_result_ts     ON calibration_result (ts DESC);
CREATE INDEX IF NOT EXISTS idx_calibration_result_bucket ON calibration_result (bucket, ts DESC);

-- Social history: daily per-token LunarCrush metrics for backtesting & live percentile ranking
CREATE TABLE IF NOT EXISTS social_history (
    id               BIGSERIAL PRIMARY KEY,
    ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    token            VARCHAR(20) NOT NULL,
    date             DATE NOT NULL,
    social_dominance DOUBLE PRECISION,
    interactions_24h DOUBLE PRECISION,
    sentiment        DOUBLE PRECISION,
    galaxy_score     DOUBLE PRECISION,
    alt_rank         INTEGER,
    price_open       DOUBLE PRECISION,
    price_close      DOUBLE PRECISION,
    price_high       DOUBLE PRECISION,
    price_low        DOUBLE PRECISION,
    volume_24h       DOUBLE PRECISION,
    UNIQUE (token, date)
);
CREATE INDEX IF NOT EXISTS idx_social_history_token_date ON social_history (token, date DESC);

-- Migrations for existing databases (safe to re-run: ADD COLUMN IF NOT EXISTS)
ALTER TABLE system_state  ADD COLUMN IF NOT EXISTS run_starting_capital NUMERIC(12,4);
ALTER TABLE decision_log  ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER;
