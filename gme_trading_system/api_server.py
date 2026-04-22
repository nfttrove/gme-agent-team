#!/usr/bin/env python3
"""
Signal-focused dashboard API server.
Endpoints for viewing signals, logging feedback, and computing metrics.
"""
import os
import sys
import sqlite3
import logging
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS

# Add parent to path for signal_manager
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gme_trading_system'))

from signal_manager import SignalManager

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'gme_trading_system', 'agent_memory.db')


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── API Endpoints ──────────────────────────────────────────────────────────

@app.route('/api/signals', methods=['GET'])
def get_signals():
    """Return recent signals from signal_alerts table."""
    limit = request.args.get('limit', 50, type=int)
    agent = request.args.get('agent', None)

    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT * FROM signal_alerts WHERE 1=1"
    params = []

    if agent:
        query += " AND agent_name = ?"
        params.append(agent)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    signals = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return jsonify(signals)


@app.route('/api/metrics', methods=['GET'])
def get_metrics():
    """Return agent metrics: win rate, count, pnl by agent."""
    agent = request.args.get('agent', None)
    days = request.args.get('days', 7, type=int)

    conn = get_db()
    cursor = conn.cursor()

    # Get date cutoff
    cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

    query = """
    SELECT
        sa.agent_name,
        COUNT(sa.id) as total_signals,
        COUNT(CASE WHEN sf.action_taken = 'executed' THEN 1 END) as executed,
        COUNT(CASE WHEN sf.action_taken = 'executed' AND sf.pnl_pct > 0 THEN 1 END) as winners,
        ROUND(AVG(CASE WHEN sf.action_taken = 'executed' THEN sf.pnl_pct END), 2) as avg_pnl_pct,
        ROUND(AVG(sa.confidence), 2) as avg_confidence
    FROM signal_alerts sa
    LEFT JOIN signal_feedback sf ON sa.id = sf.alert_id
    WHERE sa.timestamp >= ?
    """
    params = [cutoff_date]

    if agent:
        query += " AND sa.agent_name = ?"
        params.append(agent)

    query += """
    GROUP BY sa.agent_name
    ORDER BY executed DESC, total_signals DESC
    """

    cursor.execute(query, params)
    metrics = {}
    for row in cursor.fetchall():
        agent_name = row['agent_name']
        total = row['total_signals']
        executed = row['executed']
        win_rate = (row['winners'] / executed) if executed > 0 else 0

        metrics[agent_name] = {
            'total_signals': total,
            'executed': executed,
            'win_rate': f"{win_rate:.1%}",
            'avg_pnl_pct': row['avg_pnl_pct'],
            'avg_confidence': row['avg_confidence'],
        }

    conn.close()
    return jsonify(metrics)


@app.route('/api/feedback', methods=['POST'])
def log_feedback():
    """Log team feedback for a signal."""
    try:
        data = request.json

        alert_id = data.get('alert_id')
        action = data.get('action')  # executed, ignored, missed
        entry_price = data.get('entry_price', 0.0)
        exit_price = data.get('exit_price', 0.0)
        member = data.get('member', 'unknown')
        notes = data.get('notes', '')

        if not alert_id or not action:
            return jsonify({'error': 'Missing alert_id or action'}), 400

        # Compute P&L
        pnl = exit_price - entry_price if entry_price > 0 else 0
        pnl_pct = (pnl / entry_price) * 100 if entry_price > 0 else 0

        mgr = SignalManager(DB_PATH)
        mgr.log_feedback(
            alert_id=alert_id,
            action_taken=action,
            execution_timestamp=datetime.now().isoformat(),
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            team_member=member,
            team_notes=notes,
        )

        return jsonify({'status': 'ok', 'alert_id': alert_id}), 201

    except Exception as e:
        log.error(f"Feedback log failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT COUNT(*) FROM signal_alerts")
        conn.close()
        return jsonify({'status': 'ok', 'db': 'connected'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


# ── HTML Dashboard ────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GME Signal Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0f0f0f;
            color: #e0e0e0;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { margin-bottom: 30px; color: #fff; }
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            border-bottom: 1px solid #333;
        }
        .tab-btn {
            padding: 12px 20px;
            border: none;
            background: transparent;
            color: #888;
            cursor: pointer;
            font-size: 14px;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
        }
        .tab-btn.active { color: #fff; border-bottom-color: #4CAF50; }
        .tab-btn:hover { color: #ccc; }

        .tab-content { display: none; }
        .tab-content.active { display: block; }

        table {
            width: 100%;
            border-collapse: collapse;
            background: #1a1a1a;
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 20px;
        }
        th {
            background: #2a2a2a;
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #333;
            font-weight: 600;
            font-size: 13px;
            color: #aaa;
        }
        td {
            padding: 12px;
            border-bottom: 1px solid #222;
            font-size: 13px;
        }
        tr:hover { background: #222; }

        .confidence { color: #4CAF50; font-weight: 600; }
        .price { color: #64B5F6; }
        .timestamp { color: #888; font-size: 12px; }

        .form-group {
            margin-bottom: 15px;
            display: flex;
            flex-direction: column;
        }
        label { font-size: 12px; color: #aaa; margin-bottom: 5px; font-weight: 600; }
        input, select, textarea {
            padding: 10px;
            background: #2a2a2a;
            border: 1px solid #333;
            color: #e0e0e0;
            border-radius: 4px;
            font-size: 13px;
        }
        button {
            padding: 10px 20px;
            background: #4CAF50;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 600;
            font-size: 13px;
        }
        button:hover { background: #45a049; }

        .metric { background: #2a2a2a; padding: 15px; border-radius: 8px; margin-bottom: 10px; }
        .metric-label { font-size: 12px; color: #888; }
        .metric-value { font-size: 18px; color: #4CAF50; font-weight: 600; margin-top: 5px; }

        .alert { padding: 10px; border-radius: 4px; margin-bottom: 10px; }
        .alert-success { background: #1b5e20; border-left: 3px solid #4CAF50; }
        .alert-error { background: #b71c1c; border-left: 3px solid #ff5252; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎯 GME Signal Dashboard</h1>

        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('signals')">Recent Signals</button>
            <button class="tab-btn" onclick="switchTab('feedback')">Log Feedback</button>
            <button class="tab-btn" onclick="switchTab('metrics')">Metrics</button>
        </div>

        <div id="signals" class="tab-content active">
            <table id="signals-table">
                <thead>
                    <tr>
                        <th>Agent</th>
                        <th>Confidence</th>
                        <th>Entry</th>
                        <th>Stop Loss</th>
                        <th>Take Profit</th>
                        <th>Timestamp</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>

        <div id="feedback" class="tab-content">
            <h2 style="margin-bottom: 20px;">Log Execution Feedback</h2>
            <form id="feedback-form" style="max-width: 500px;">
                <div class="form-group">
                    <label>Alert ID</label>
                    <input type="text" id="alert_id" placeholder="e.g. abc-123" required>
                </div>
                <div class="form-group">
                    <label>Action Taken</label>
                    <select id="action" required>
                        <option value="">Select...</option>
                        <option value="executed">Executed</option>
                        <option value="ignored">Ignored</option>
                        <option value="missed">Missed (saw opportunity too late)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Entry Price (USD)</label>
                    <input type="number" id="entry_price" step="0.01" placeholder="23.45">
                </div>
                <div class="form-group">
                    <label>Exit Price (USD)</label>
                    <input type="number" id="exit_price" step="0.01" placeholder="24.50">
                </div>
                <div class="form-group">
                    <label>Team Member</label>
                    <input type="text" id="member" placeholder="Your name">
                </div>
                <div class="form-group">
                    <label>Notes (optional)</label>
                    <textarea id="notes" rows="3" placeholder="Any context..."></textarea>
                </div>
                <button type="submit">Log Feedback</button>
            </form>
            <div id="feedback-alert" style="margin-top: 20px;"></div>
        </div>

        <div id="metrics" class="tab-content">
            <h2 style="margin-bottom: 20px;">Agent Performance (Last 7 Days)</h2>
            <div id="metrics-container"></div>
        </div>
    </div>

    <script>
        function switchTab(name) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            event.target.classList.add('active');

            if (name === 'signals') loadSignals();
            if (name === 'metrics') loadMetrics();
        }

        function loadSignals() {
            fetch('/api/signals?limit=50')
                .then(r => r.json())
                .then(signals => {
                    const tbody = document.querySelector('#signals-table tbody');
                    tbody.innerHTML = signals.map(s => `
                        <tr>
                            <td>${s.agent_name}</td>
                            <td class="confidence">${(s.confidence * 100).toFixed(0)}%</td>
                            <td class="price">$${s.entry_price.toFixed(2)}</td>
                            <td class="price">$${s.stop_loss.toFixed(2)}</td>
                            <td class="price">$${s.take_profit.toFixed(2)}</td>
                            <td class="timestamp">${new Date(s.timestamp).toLocaleString()}</td>
                        </tr>
                    `).join('');
                })
                .catch(e => console.error(e));
        }

        function loadMetrics() {
            fetch('/api/metrics')
                .then(r => r.json())
                .then(metrics => {
                    const container = document.getElementById('metrics-container');
                    container.innerHTML = Object.entries(metrics).map(([agent, m]) => `
                        <div class="metric">
                            <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px;">
                                <div>
                                    <div class="metric-label">${agent}</div>
                                    <div class="metric-value">${m.total_signals}</div>
                                    <div class="metric-label">signals</div>
                                </div>
                                <div>
                                    <div class="metric-label">Executed</div>
                                    <div class="metric-value">${m.executed}</div>
                                </div>
                                <div>
                                    <div class="metric-label">Win Rate</div>
                                    <div class="metric-value">${m.win_rate}</div>
                                </div>
                                <div>
                                    <div class="metric-label">Avg P&L</div>
                                    <div class="metric-value" style="color: ${m.avg_pnl_pct > 0 ? '#4CAF50' : '#ff5252'}">${m.avg_pnl_pct ? m.avg_pnl_pct.toFixed(2) + '%' : '—'}</div>
                                </div>
                            </div>
                        </div>
                    `).join('');
                })
                .catch(e => console.error(e));
        }

        document.getElementById('feedback-form').addEventListener('submit', e => {
            e.preventDefault();
            const payload = {
                alert_id: document.getElementById('alert_id').value,
                action: document.getElementById('action').value,
                entry_price: parseFloat(document.getElementById('entry_price').value) || 0,
                exit_price: parseFloat(document.getElementById('exit_price').value) || 0,
                member: document.getElementById('member').value,
                notes: document.getElementById('notes').value,
            };

            fetch('/api/feedback', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
                .then(r => r.json())
                .then(data => {
                    const alert = document.getElementById('feedback-alert');
                    if (data.status === 'ok') {
                        alert.innerHTML = `<div class="alert alert-success">✅ Feedback logged for ${data.alert_id}</div>`;
                        document.getElementById('feedback-form').reset();
                    } else {
                        alert.innerHTML = `<div class="alert alert-error">❌ Error: ${data.error}</div>`;
                    }
                    setTimeout(() => alert.innerHTML = '', 5000);
                })
                .catch(e => {
                    document.getElementById('feedback-alert').innerHTML = `<div class="alert alert-error">❌ Error: ${e.message}</div>`;
                });
        });

        // Load signals on page load
        loadSignals();
    </script>
</body>
</html>
"""


@app.route('/')
def dashboard():
    """Serve dashboard HTML."""
    return render_template_string(DASHBOARD_HTML)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
