# Premium-Selling: 0DTE Iron Condor & Credit Spread System

## Overview
Tastylive-style 0DTE premium selling system that trades iron condors on range days
and directional credit spreads on trend days. Targets SPY options via Alpaca paper trading.

## Tech Stack
- Python 3.12, AWS Lambda (Docker), CDK for IaC
- alpaca-py for trading + market data, yfinance for VIX
- DynamoDB (positions, trades, market state), S3 (daily summaries), SNS (alerts)

## Architecture
- 5 Lambda functions: scanner, executor, monitor, closer, reconciler
- Step Functions workflow with 5-minute monitor loop (9:35 AM - 4:15 PM ET)
- Single shared Docker image, per-function CMD override

## Strategy
- **Range days**: Iron condor (sell put spread + call spread outside expected move)
- **Trend days**: Directional credit spread (sell spread with the trend)
- Market regime detected by 10:00 AM via VWAP, opening range, candle analysis, VIX
- Risk: 2x stop loss, 50% take profit, 3:45 PM force close

## Key Commands
```bash
# Run tests
pytest tests/unit/ -v

# Deploy
cd cdk && cdk deploy --all

# Seed secrets
python scripts/seed_secrets.py
```

## Conventions
- Follow common serverless patterns (Lambda, Step Functions, DynamoDB)
- Frozen dataclasses for config, mutable for positions
- DynamoDB Decimal serialization for floats
- All times in US/Eastern for trading logic
