#!/usr/bin/env python3
"""Seed Alpaca API credentials into AWS Secrets Manager.

Usage:
    export AWS_DEFAULT_REGION=us-east-1      # or AWS_REGION
    export ALPACA_API_KEY=your-key
    export ALPACA_API_SECRET=your-secret
    python scripts/seed_secrets.py

AWS credentials come from the usual boto3 sources, for example AWS_PROFILE.
"""

import json
import os
import sys

import boto3

SECRET_NAME = "premium/spy-api"


def main():
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")

    if not api_key or not api_secret:
        print("Error: Set ALPACA_API_KEY and ALPACA_API_SECRET environment variables")
        sys.exit(1)

    secret_value = json.dumps(
        {
            "api_key": api_key,
            "api_secret": api_secret,
        }
    )

    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    if not region:
        print("Error: Set AWS_DEFAULT_REGION (or AWS_REGION) to the region you deploy to")
        sys.exit(1)

    client = boto3.client("secretsmanager", region_name=region)

    try:
        client.create_secret(
            Name=SECRET_NAME,
            SecretString=secret_value,
            Description="Alpaca paper trading API credentials for premium-selling",
        )
        print(f"Created secret: {SECRET_NAME}")
    except client.exceptions.ResourceExistsException:
        client.update_secret(
            SecretId=SECRET_NAME,
            SecretString=secret_value,
        )
        print(f"Updated secret: {SECRET_NAME}")


if __name__ == "__main__":
    main()
