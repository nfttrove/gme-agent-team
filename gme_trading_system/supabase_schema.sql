-- Run this once in the Supabase SQL Editor:
-- https://supabase.com/dashboard/project/vhxdcoktggucxyqcnfsc/sql/new
--
-- Uses BIGINT primary keys to match SQLite row ids (not auto-generated),
-- so upserts are idempotent on restart.

CREATE TABLE IF NOT EXISTS agent_logs (
    id          BIGINT PRIMARY KEY,
    agent_name  TEXT NOT NULL,
    timestamp   TEXT,
    task_type   TEXT,
    content     TEXT,
    status      TEXT DEFAULT 'ok',
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id          BIGINT PRIMARY KEY,
    order_id    TEXT UNIQUE NOT NULL,
    timestamp   TEXT,
    action      TEXT,
    symbol      TEXT DEFAULT 'GME',
    quantity    REAL,
    entry_price REAL,
    stop_loss   REAL,
    take_profit REAL,
    confidence  REAL,
    approved_by TEXT,
    status      TEXT DEFAULT 'pending',
    paper_trade INT  DEFAULT 1,
    exit_price  REAL,
    pnl         REAL,
    notes       TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS predictions (
    id              BIGINT PRIMARY KEY,
    timestamp       TEXT,
    horizon         TEXT,
    predicted_price REAL,
    confidence      REAL,
    reasoning       TEXT,
    actual_price    REAL,
    error_pct       REAL,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stream_comments (
    id        BIGINT PRIMARY KEY,
    timestamp TEXT,
    comment   TEXT NOT NULL,
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS structural_signals (
    id               BIGINT PRIMARY KEY,
    timestamp        TEXT,
    ticker           TEXT NOT NULL,
    signal_name      TEXT NOT NULL,
    filing_type      TEXT,
    filing_date      TEXT,
    headline         TEXT,
    url              TEXT,
    confidence       REAL,
    action           TEXT,
    timeline_months  INT,
    synced_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Enable Row Level Security (recommended for Supabase)
ALTER TABLE agent_logs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade_decisions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE predictions         ENABLE ROW LEVEL SECURITY;
ALTER TABLE stream_comments     ENABLE ROW LEVEL SECURITY;
ALTER TABLE structural_signals  ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (used by the sync module)
CREATE POLICY "service_all" ON agent_logs        FOR ALL USING (true);
CREATE POLICY "service_all" ON trade_decisions    FOR ALL USING (true);
CREATE POLICY "service_all" ON predictions        FOR ALL USING (true);
CREATE POLICY "service_all" ON stream_comments    FOR ALL USING (true);
CREATE POLICY "service_all" ON structural_signals FOR ALL USING (true);
