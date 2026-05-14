"""
Tiny dev runner for the OBS stats panel only.

Production serves /obs/stats from logger_daemon.py:8765 alongside everything
else (TradingView webhook, Prometheus metrics, etc). This file exists so the
panel can be previewed in isolation on a non-conflicting port without
starting all the background feeds.

Run:
    python obs_dev_server.py --port 8766
"""
import argparse
import os

from flask import Flask, jsonify, render_template

from obs_panel_data import assemble_panel_payload


def make_app() -> Flask:
    here = os.path.dirname(__file__)
    app = Flask(
        __name__,
        template_folder=os.path.join(here, "templates"),
        static_folder=os.path.join(here, "static"),
        static_url_path="/obs/static",
    )

    @app.route("/obs/stats")
    def obs_stats():
        return render_template("obs_stats.html")

    @app.route("/obs/stats.json")
    def obs_stats_json():
        return jsonify(assemble_panel_payload())

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    make_app().run(host="127.0.0.1", port=args.port, debug=False)
