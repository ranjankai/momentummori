# Legacy — superseded by V4, kept for reference only

Nothing here is on the live path. `run_strategy.py` does not import any
of it, and no scheduled task runs it.

- `app.py`      stdlib wsgiref dashboard over the old 4-signal pipeline
- `pipeline.py` the 4-signal model (rollover / carry / price momentum /
                volume trend), superseded by `strategy.py`'s V4
- `backtest.py`, `run_backtest.py`  backtests for that old model
- `static/`     the dashboard's single HTML page

These read `config/universe.csv` (192 hand-seeded symbols), which
`fo_mktlots.csv` replaced. The `SIGNAL_WEIGHTS` block in `config.py`
exists only for these files.

Delete the lot once you are sure you want nothing from it.
