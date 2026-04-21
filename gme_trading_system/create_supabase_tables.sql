-- Run this in Supabase SQL editor to create the tables

CREATE TABLE IF NOT EXISTS daily_candles (
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  open DECIMAL(10, 2),
  high DECIMAL(10, 2),
  low DECIMAL(10, 2),
  close DECIMAL(10, 2),
  volume BIGINT,
  vwap DECIMAL(10, 2),
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE(symbol, date)
);

CREATE TABLE IF NOT EXISTS price_ticks (
  id BIGSERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  open DECIMAL(10, 2),
  high DECIMAL(10, 2),
  low DECIMAL(10, 2),
  close DECIMAL(10, 2),
  volume BIGINT,
  source TEXT,
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE(symbol, timestamp, source)
);

CREATE INDEX IF NOT EXISTS idx_daily_candles_symbol_date ON daily_candles(symbol, date DESC);
CREATE INDEX IF NOT EXISTS idx_price_ticks_symbol_timestamp ON price_ticks(symbol, timestamp DESC);
