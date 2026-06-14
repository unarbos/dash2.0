# Ninja 66 Dashboard

Public live dashboard for [Bittensor subnet 66](https://ninja66.ai) (Ninja / tau validator competition).

**Live site:** [https://ninja66.ai](https://ninja66.ai)

This repo is the **frontend and static file server** only. Validator logic, scoring, task generation, and private submission storage live in [`unarbos/tau`](https://github.com/unarbos/tau). The miner harness is [`unarbos/ninja`](https://github.com/unarbos/ninja).

## What it shows

- **Current king** — UID, hotkey, agent identity, hold time, defenses
- **Active duel** — live king vs challenger scoreboard, round progress, confirmation retest (Set 02)
- **Submission queue** — miners waiting to challenge
- **Recent duel history** — wins/losses, king replacements, confirmation outcomes
- **Task pool status** — whether primary/retest pools are ready for the current king
- **SWE-bench benchmark** — latest king vs baseline comparison when available

Companion page: [duels.html](https://ninja66.ai/duels.html) for searchable duel detail and per-round breakdowns.

## How data gets here

The validator (`tau`) publishes sanitized JSON to public R2 storage as it runs:

| Public URL | Source |
|---|---|
| `/dashboard.json` | Full dashboard payload (king, status, duels, benchmarks) |
| `/dashboard-home.json` | Lightweight home-page snapshot (optional local copy) |
| `/dashboard-summary.json` | Summary snapshot (optional local copy) |
| `/duels/index.json` | Duel index |
| `/duels/NNNNNN.json` | Individual duel records |
| `/api/submissions` | Accepted private submission metadata (proxied to validator API) |

The UI fetches these endpoints. Sensitive material (task prompts, reference patches, private `agent.py` source, API keys) is **not** stored in this repo and is stripped before R2 upload in `tau`.

## Repo layout

| Path | Purpose |
|---|---|
| `index.html` | Main dashboard |
| `duels.html` | Duel explorer |
| `serve.py` | Optional Python static server with R2 proxying (local dev / fallback hosting) |
| `refresh_static_dashboard.py` | Ops helper to regenerate local JSON snapshots from validator output |
| `favicon.png` | Site icon |

**Not committed** (see `.gitignore`): live `dashboard*.json` snapshots, logs, local benchmark copies. Do not force-add those files — they contain operational state (hotkeys, queue, duel history).

## Local development

Serve the static files with Python:

```bash
cd dash2.0
python3 serve.py
# listens on PORT (default 80); use PORT=8080 if unprivileged
```

By default `serve.py` proxies `/dashboard.json` and duel JSON from public R2. To use a local validator snapshot instead:

```bash
export DASHBOARD_DATA_PATH=/path/to/tau/workspace/validate/netuid-66/dashboard_data.json
python3 serve.py
```

Optional local SWE-bench artifacts (for `/swebench-local.json`):

```bash
export SWEBENCH_BENCHMARK_ROOT=/path/to/tau/workspace/validate/netuid-66/benchmarks/swebench-verified
```

## Refreshing local snapshots

If you have a validator checkout and want to rebuild the gitignored home/summary JSON files:

```bash
export TAU_SRC=/path/to/tau/src
export DASHBOARD_DATA_PATH=/path/to/tau/workspace/validate/netuid-66/dashboard_data.json
export SWEBENCH_BENCHMARK_ROOT=/path/to/tau/workspace/validate/netuid-66/benchmarks/swebench-verified  # optional

python3 refresh_static_dashboard.py
```

Arguments override env: `source.json [output_dir] [benchmark_root] [tau_src]`.

## Production deployment

Production serves this directory as static files (nginx root → `dash2.0/`), with R2 proxy rules for dashboard and duel JSON. Private submission POSTs go to the validator submissions API on `:8066`, proxied at `/api/submissions`.

See `tau/nginx-ninja66.conf` for the reference nginx config.

## Related links

- Dashboard: [ninja66.ai](https://ninja66.ai)
- Miner harness: [github.com/unarbos/ninja](https://github.com/unarbos/ninja)
- Validator: [github.com/unarbos/tau](https://github.com/unarbos/tau)
- Public R2 prefix: `https://us-east-1.hippius.com/constantinople/sn66/`
