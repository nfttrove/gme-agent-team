# OBS static assets

Drop your assets here. The HTML at `/obs/stats` references them via `/obs/static/<filename>`:

- `pirate.gif` — animated pirate-flag GIF rendered top-right of the stats card.
- `subscribe.png` — static YouTube-subscribe CTA rendered at the bottom of the card.

If a file is missing the `<img>` tag hides itself silently (via `onerror`), so the
panel still renders — you can add assets at any time without touching the template.

To swap or remove either element, edit `gme_trading_system/templates/obs_stats.html`
directly.
