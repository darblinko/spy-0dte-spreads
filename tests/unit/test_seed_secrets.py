"""scripts/seed_secrets.py against moto Secrets Manager."""

import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_secrets.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("seed_secrets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_env(monkeypatch):
    for name in ("AWS_DEFAULT_REGION", "AWS_REGION", "AWS_PROFILE", "AWS_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    return monkeypatch


def test_region_from_aws_region_alone(seed_env):
    seed_env.setenv("AWS_REGION", "us-west-2")
    with mock_aws():
        _load_script().main()
        stored = boto3.client("secretsmanager", region_name="us-west-2").get_secret_value(
            SecretId="premium/spy-api"
        )
    assert json.loads(stored["SecretString"]) == {"api_key": "key", "api_secret": "secret"}


def test_missing_region_exits_with_an_error(seed_env, capsys):
    with mock_aws(), pytest.raises(SystemExit) as exit_info:
        _load_script().main()
    assert exit_info.value.code == 1
    assert "AWS_DEFAULT_REGION" in capsys.readouterr().out
