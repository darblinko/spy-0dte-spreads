# premium-selling

Same-day (0DTE) SPY option spread system on AWS: five Lambda functions on one container image, run by Step Functions and EventBridge Scheduler, with positions and trades in DynamoDB, deployed as four CDK stacks.

Status: incomplete prototype for a paper trading account (`paper=True` in `src/common/alpaca_clients.py`). The order lifecycle is tested offline, against broker fakes built from alpaca-py response models and an in-memory DynamoDB (moto). The repository records no run against the broker or a deployed stack.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/figures/system-at-a-glance-dark.svg">
  <img alt="4 CDK stacks, 5 Lambda functions, 4 schedules, 244 offline tests passing." src="docs/figures/system-at-a-glance-light.svg" width="760">
</picture>

<details><summary>Data table</summary>

| Measure | Value |
|---|---|
| CDK stacks | 4 |
| Lambda functions | 5 |
| Schedules | 4 |
| Offline tests passing | 244 |

Source: `cdk/app.py`, `cdk/stacks/compute_stack.py`, `cdk/stacks/orchestration_stack.py`, and `pytest tests/unit tests/cdk` at revision `c2b4942`. Chart data: `docs/data.json`.

</details>

## What it does

- Classifies each trading day at 10:00 Eastern as range, uptrend, downtrend or uncertain from one-minute SPY bars since the open: VWAP crosses, position against VWAP, opening range breakout and candle body ratio (`src/market/regime.py`). The bar handling is tested with the alpaca-py `BarSet` model (`tests/unit/test_scanner_bars.py`).
- Defines two strategies on one paper trading account (`src/common/config.py`). `IC_SPY` sells an iron condor on range days. `CS_SPY` sells a credit spread in the direction of the trend on trend days.
- Picks the short strike by delta and the long strike a fixed width away, sizes the order from the account equity and the remaining daily loss budget, and submits the spread as one multi-leg limit order whose client order ID is derived from the scan. A position is recorded as open only after the broker reports a fill (`src/strategy/`, `src/executor/handler.py`, `src/common/lifecycle.py`).
- Checks active positions every 5 minutes and starts a close when the spread value reaches 2 times the credit received or falls to half of it. Every leg must have a quote no older than 2 minutes; otherwise the position is not evaluated (`src/monitor/handler.py`).
- Starts a close for every open position once the session close deadline passes: 15 minutes before the next close reported by the broker clock, 15:45 Eastern on a regular day (`src/common/session.py`). The closer at 15:45 is a backstop (`src/closer/handler.py`). A position counts as closed only after its close order fills. The closer reports positions that are still active on the error topic.
- Writes a daily summary to S3 at 16:15 Eastern and sends it over SNS. The summary compares the broker's option positions and working orders with the stored positions and lists every discrepancy (`src/reconciler/handler.py`).

## How it works

```mermaid
flowchart TB
  sched["EventBridge Scheduler<br/>4 schedules, America/New_York"]

  subgraph entry["Entry state machine, 10:00 Mon to Fri"]
    direction TB
    scanIC["Scanner Lambda<br/>IC_SPY"] -- should_trade --> execIC["Executor Lambda<br/>iron condor"]
    scanCS["Scanner Lambda<br/>CS_SPY"] -- should_trade --> execCS["Executor Lambda<br/>credit spread"]
  end
  subgraph manage["Position management"]
    monitor["Monitor Lambda<br/>every 5 minutes, through the monitor state machine"]
    closer["Closer Lambda<br/>15:45 Mon to Fri"]
  end
  recon["Reconciler Lambda<br/>16:15 Mon to Fri"]

  sched --> entry
  sched --> monitor
  sched --> closer
  sched --> recon

  yf["yfinance<br/>VIX"]
  alpaca["Alpaca API<br/>clock, bars, option quotes, orders"]
  ddb[("DynamoDB<br/>positions, trades, market state")]
  s3[("S3<br/>daily-summaries/")]
  sns["SNS<br/>trade and error topics"]

  scanIC & scanCS --> yf
  entry --> alpaca
  manage --> alpaca
  entry --> ddb
  manage --> ddb
  recon --> alpaca
  recon --> ddb
  recon --> s3
  entry & manage & recon -.-> sns
```

Every function reads the broker keys from the Secrets Manager secret `premium/spy-api` (`src/common/alpaca_clients.py`, `src/common/config.py`). Each function's role can read only that secret and the tables, topics and bucket actions its handler uses (`cdk/stacks/compute_stack.py`).

The five functions share the image built from `src/Dockerfile`; each overrides the command (`cdk/stacks/compute_stack.py`).

| Function | Trigger | Memory, timeout | Work |
|---|---|---|---|
| Scanner | Entry state machine, once per strategy | 1,024 MB, 120 s | Stops if the market is closed, the strategy has 3 active positions (pending, open or closing) or there are fewer than 5 bars. Otherwise classifies the regime and returns `should_trade` and a `scan_id` when the regime matches the strategy with confidence of at least 0.6 (`src/scanner/handler.py`). |
| Executor | Entry state machine, when `should_trade` is true | 512 MB, 120 s | Stops outside the 10:00 to 14:30 entry window, after the close deadline or at the daily loss limit. Otherwise requests quotes for 15 strikes on each side of the money, builds and sizes the spread, stores it as pending under its client order ID, submits the order and records the position and an open trade from the fill (`src/executor/handler.py`). |
| Monitor | Monitor state machine | 512 MB, 60 s | Returns when nothing is active, and without trading when the broker clock reports the market closed. Otherwise settles pending entries and closes, prices each leg at the mid and starts a close on the stop or take profit threshold or after the close deadline (`src/monitor/handler.py`). |
| Closer | Scheduler, 15:45 | 512 MB, 120 s | Runs the monitor pass with a close for every open position and alerts on positions still active (`src/closer/handler.py`). |
| Reconciler | Scheduler, 16:15 | 512 MB, 120 s | Builds the daily summary in dollars from the trades table, compares broker holdings and working orders with the stored positions and writes the summary to S3 (`src/reconciler/handler.py`). |

The three cron schedules carry the time zone `America/New_York`, so they follow daylight saving time. The monitor schedule is `rate(5 minutes)` (`cdk/stacks/orchestration_stack.py`).

Storage (`cdk/stacks/storage_stack.py`, `src/common/dynamo_io.py`):

| Resource | Keys | Retention |
|---|---|---|
| DynamoDB `premium-positions` | `strategy`, `spread_id` | Point-in-time recovery; retained on stack delete |
| DynamoDB `premium-trades` | `strategy`, `trade_id` | Point-in-time recovery; retained on stack delete |
| DynamoDB `premium-market-state` | `date`, `timestamp` | Items expire 30 days after they are written (`ttl_epoch`) |
| S3 `premium-selling-data-<account id>` | `daily-summaries/<date>.json` | Versioned. Current versions expire after 365 days, noncurrent versions 30 days after they are replaced or expire; expired delete markers are removed. Summaries stay in S3 Standard (objects under 128 KB do not transition to Infrequent Access by default) |

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). The CDK tests also need Node.js.

```bash
git clone https://github.com/darblinko/spy-0dte-spreads
cd spy-0dte-spreads
uv run --group dev python -m pytest                              # 222 unit tests, no AWS access
uv run --group cdk --group dev python -m pytest tests/cdk        # 22 CDK synthesis tests
uvx ruff check . && uvx ruff format --check .
```

Deploying needs an AWS account, Docker and the CDK CLI (`npm install -g aws-cdk`). AWS credentials come from the usual sources, for example `AWS_PROFILE` or `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The broker keys and the region are read from the environment; the variable names are in `.env.example`.

```bash
export AWS_PROFILE=<profile>                 # or AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
export AWS_DEFAULT_REGION=us-east-1          # the seed script exits if no region is set
export ALPACA_API_KEY=<key> ALPACA_API_SECRET=<secret>
uv sync --group cdk && source .venv/bin/activate
python scripts/seed_secrets.py               # writes the broker keys to premium/spy-api
cd cdk
cdk bootstrap aws://<account id>/us-east-1   # once per account and region
cdk deploy --all --context account=<account id> --context region=us-east-1
```

`--context alert_email=<address>` subscribes an address to the trade and error topics. The image installs `src/requirements.txt`, which is exported from `uv.lock` with hashes (`uv export --frozen --no-default-groups --no-emit-project --format requirements-txt -o src/requirements.txt`); CI checks that it matches the lock.

The trading client is created with `paper=True` (`src/common/alpaca_clients.py`). The schedules are enabled on deploy.

## Project layout

```
cdk/
  app.py                  four stacks: Storage, Observability, Compute, Orchestration
  stacks/                 one module per stack
src/
  Dockerfile              shared image for all five functions
  common/                 config, models, DynamoDB and S3 access, broker clients, SNS, metrics
  market/                 VWAP, opening range, expected move, regime classification
  strategy/               strike selection, iron condor, credit spread
  scanner/ executor/      entry workflow functions
  monitor/ closer/        position management functions
  reconciler/             end-of-day summary
scripts/
  seed_secrets.py         environment to Secrets Manager
tests/
  unit/                   222 tests: moto for AWS, broker fakes from alpaca-py models
  cdk/                    22 synthesis tests
docs/                     chart data, README figures, landing page
```

## Design decisions and limitations

- `regime_detection_time` in `src/common/config.py` is not read by any code. The scan time is set by the entry schedule.
- Entries and closes are confirmed from broker order states and fills. That handling is tested against offline fakes only (`tests/unit/test_entry_lifecycle.py`, `tests/unit/test_monitor_lifecycle.py`), not against the broker. A close order that does not fill returns the position to open, and the monitor tries again on its next run. A position still active after the closer is reported on the error topic; one still open after its expiration date is reported again by each monitor run and has to be resolved by hand.
- Credits and P&L in trades and reports are dollars before fees: quote points times quantity times the contract multiplier of 100 (`src/common/models.py`).
- Active positions are found by reading every position of each strategy partition, page by page. Closed positions stay in the table, so this read grows with the history (`src/common/dynamo_io.py`).
- VIX is fetched from yfinance and stored with the regime signals, but it does not enter the regime score (`src/market/regime.py`). A failed fetch uses 20.0 (`src/scanner/handler.py`).
- The CloudWatch dashboard plots four custom metrics in the `Premium/Trading` namespace (`cdk/stacks/observability_stack.py`). The scanner, monitor and reconciler publish them; a failed publish is logged and does not stop the run (`src/common/metrics.py`). Each function has an alarm on Lambda errors that notifies the error topic. The topics have a subscriber only when deployed with `--context alert_email=<address>`.
- The monitor schedule fires 288 times a day on every day of the week. A run with no active positions makes no broker calls; otherwise the broker clock decides whether the market is open, which covers weekends, holidays and early closes.
- Both strategies use the same secret and therefore the same broker account (`src/common/config.py`).
- The repository contains no backtest, no simulation and no trading record. Nothing in it shows that the strategies have an edge.
- CI runs only offline tests. There are no integration tests against a deployed stack.

## Not investment advice

This repository is published as a software engineering example. It is not investment advice, a recommendation to trade any security or strategy, or an offer of any service. Options trading can lose more than the credit received, and trading can lose the capital invested. The code comes with no warranty; see the license. Anyone who runs it against a brokerage account does so at their own risk.

## License

Apache-2.0. See [LICENSE](LICENSE).
