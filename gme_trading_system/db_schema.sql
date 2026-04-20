CREATE TABLE IF NOT EXISTS price_ticks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT    NOT NULL DEFAULT 'GME',
    timestamp TEXT    NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    REAL,
    source    TEXT    DEFAULT 'tradingview',
    UNIQUE(symbol, timestamp)
);

CREATE TABLE IF NOT EXISTS daily_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL DEFAULT 'GME',
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    vwap REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trend_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    support_level REAL,
    resistance_level REAL,
    trend_direction TEXT,
    strength REAL,
    notes TEXT,
    agent TEXT
);

CREATE TABLE IF NOT EXISTS news_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    headline TEXT,
    source TEXT,
    sentiment_score REAL,
    sentiment_label TEXT,
    relevance_score REAL,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    horizon TEXT NOT NULL,
    predicted_price REAL,
    confidence REAL,
    reasoning TEXT,
    actual_price REAL,
    error_pct REAL
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT    NOT NULL UNIQUE,          -- UUID4, prevents double-execution
    timestamp   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    symbol      TEXT    DEFAULT 'GME',
    quantity    REAL,
    entry_price REAL,
    stop_loss   REAL,
    take_profit REAL,
    confidence  REAL,
    approved_by TEXT,
    status      TEXT    DEFAULT 'pending',
    paper_trade INTEGER DEFAULT 1,
    exit_price  REAL,
    pnl         REAL,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    task_type TEXT,
    content TEXT,
    status TEXT DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS stream_comments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    comment   TEXT    NOT NULL,
    displayed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS data_quality_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    check_type  TEXT,
    result      TEXT,
    anomalies   TEXT,
    status      TEXT DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS performance_scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    agent_name  TEXT    NOT NULL,
    metric      TEXT    NOT NULL,     -- 'prediction_error_pct', 'win_rate', 'avg_pnl', 'cycle_duration_s'
    value       REAL    NOT NULL,
    sample_size INTEGER DEFAULT 0,
    notes       TEXT,
    UNIQUE(date, agent_name, metric)
);

CREATE TABLE IF NOT EXISTS strategy_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    parameter   TEXT    NOT NULL,    -- e.g. 'long_entry.rsi14', 'exit_conditions.long.hard_stop_pct'
    old_value   REAL,
    new_value   REAL,
    reason      TEXT,
    approved_by TEXT    DEFAULT 'Boss',
    reverted    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS learning_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    session_type TEXT   NOT NULL,    -- 'daily_debrief', 'weekly_review'
    summary     TEXT,
    changes_made INTEGER DEFAULT 0,
    status      TEXT    DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS structural_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    ticker           TEXT    NOT NULL,
    signal_name      TEXT    NOT NULL,
    filing_type      TEXT,               -- 8-K, DEF 14A, Form 4, 13D, etc.
    filing_date      TEXT,
    headline         TEXT,
    url              TEXT,
    confidence       REAL,
    action           TEXT,               -- SHORT, SQUEEZE_WATCH, EXIT, MONITOR
    timeline_months  INTEGER,
    reviewed         INTEGER DEFAULT 0,
    UNIQUE(ticker, signal_name, filing_date)
);

CREATE TABLE IF NOT EXISTS short_watchlist (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT    NOT NULL UNIQUE,
    company_name     TEXT,
    added_date       TEXT    NOT NULL,
    signal_score     INTEGER DEFAULT 0,   -- 0-100 composite PE score
    confidence       REAL    DEFAULT 0.0,
    action           TEXT    DEFAULT 'WATCH',  -- SHORT, WATCH, PASS
    timeline_months  INTEGER,
    notes            TEXT,
    active           INTEGER DEFAULT 1,
    last_updated     TEXT
);

CREATE INDEX IF NOT EXISTS idx_stream_comments_displayed ON stream_comments(displayed);
CREATE INDEX IF NOT EXISTS idx_price_ticks_timestamp ON price_ticks(timestamp);
CREATE INDEX IF NOT EXISTS idx_daily_candles_date ON daily_candles(date);
CREATE INDEX IF NOT EXISTS idx_predictions_timestamp ON predictions(timestamp);
CREATE INDEX IF NOT EXISTS idx_trade_decisions_timestamp ON trade_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_performance_scores_date ON performance_scores(date);
CREATE INDEX IF NOT EXISTS idx_strategy_history_timestamp ON strategy_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_structural_signals_ticker ON structural_signals(ticker);
CREATE INDEX IF NOT EXISTS idx_structural_signals_date ON structural_signals(filing_date);
