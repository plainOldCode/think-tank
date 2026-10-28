# think-tank turbo

Headless runner for think-tank cards. The board is the only place a person answers. [TURBO.md](TURBO.md) lists what this branch keeps, what it dropped, and how to run it.

```bash
pip install fastapi uvicorn
export TURBO_DB=data/turbo.db
export TT_LEGACY_DB=/path/to/the/existing/tt.sqlite
python -m turbo
```

The page at http://127.0.0.1:7810/ reads `TT_LEGACY_DB` and does not write to it. Turbo cards are stored in `TURBO_DB`.
