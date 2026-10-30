"""Synthesis tests for the CDK stacks. They need the cdk dependency group and Node.js."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from stacks.compute_stack import ComputeStack
from stacks.observability_stack import ObservabilityStack
from stacks.orchestration_stack import OrchestrationStack
from stacks.storage_stack import StorageStack

FUNCTION_ENV_KEYS = [
    "S3_BUCKET",
    "POSITIONS_TABLE",
    "TRADES_TABLE",
    "MARKET_STATE_TABLE",
    "SNS_TRADE_TOPIC",
    "SNS_ERROR_TOPIC",
]


@pytest.fixture(scope="module")
def templates() -> dict[str, Template]:
    app = cdk.App()
    storage = StorageStack(app, "Storage")
    observability = ObservabilityStack(app, "Observability")
    compute = ComputeStack(app, "Compute", storage=storage, observability=observability)
    orchestration = OrchestrationStack(
        app, "Orchestration", compute=compute, observability=observability
    )
    stacks = {
        "storage": storage,
        "observability": observability,
        "compute": compute,
        "orchestration": orchestration,
    }
    return {name: Template.from_stack(stack) for name, stack in stacks.items()}


def test_storage_has_one_bucket_and_three_tables(templates):
    templates["storage"].resource_count_is("AWS::S3::Bucket", 1)
    templates["storage"].resource_count_is("AWS::DynamoDB::Table", 3)


def test_five_functions_share_the_environment(templates):
    compute = templates["compute"]
    compute.resource_count_is("AWS::Lambda::Function", 5)
    env_matcher = Match.object_like({key: Match.any_value() for key in FUNCTION_ENV_KEYS})
    functions = compute.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Environment": {"Variables": env_matcher}}},
    )
    assert len(functions) == 5


def test_two_workflows_on_four_schedules(templates):
    orchestration = templates["orchestration"]
    orchestration.resource_count_is("AWS::StepFunctions::StateMachine", 2)
    orchestration.resource_count_is("AWS::Scheduler::Schedule", 4)


def test_cron_schedules_use_eastern_time(templates):
    eastern = templates["orchestration"].find_resources(
        "AWS::Scheduler::Schedule",
        {"Properties": {"ScheduleExpressionTimezone": "America/New_York"}},
    )
    assert len(eastern) == 3


def test_observability_has_two_topics_and_a_dashboard(templates):
    observability = templates["observability"]
    observability.resource_count_is("AWS::SNS::Topic", 2)
    observability.resource_count_is("AWS::CloudWatch::Dashboard", 1)


def _function_ids(template: Template) -> dict[str, str]:
    functions = template.find_resources("AWS::Lambda::Function")
    return {props["Properties"]["FunctionName"]: logical for logical, props in functions.items()}


def _image_command(template: Template, function_name: str) -> list[str]:
    logical = _function_ids(template)[function_name]
    resource = template.find_resources("AWS::Lambda::Function")[logical]
    return resource["Properties"]["ImageConfig"]["Command"]


def _granted(template: Template, function_name: str) -> set[tuple[str, str]]:
    """(action, resource kind) pairs in the policies attached to the function's role."""
    function = template.find_resources("AWS::Lambda::Function")[
        _function_ids(template)[function_name]
    ]
    role = function["Properties"]["Role"]["Fn::GetAtt"][0]
    kinds = ["PositionsTable", "TradesTable", "MarketStateTable", "DataBucket", "BrokerSecret"]
    kinds += ["TradeAlerts", "ErrorAlerts"]
    grants = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role} not in policy["Properties"]["Roles"]:
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            resource_text = str(statement["Resource"])
            matched = [k for k in kinds if k in resource_text] or [resource_text]
            if "secretsmanager" in str(actions) and "premium/spy-api" in resource_text:
                matched = ["BrokerSecret"]
            grants |= {(action, kind) for action in actions for kind in matched}
    return grants


@pytest.mark.parametrize(
    "function_name, command",
    [
        ("premium-scanner", "scanner.handler.handler"),
        ("premium-executor", "executor.handler.handler"),
        ("premium-monitor", "monitor.handler.handler"),
        ("premium-closer", "closer.handler.handler"),
        ("premium-reconciler", "reconciler.handler.handler"),
    ],
)
def test_each_function_runs_its_own_handler(templates, function_name, command):
    assert _image_command(templates["compute"], function_name) == [command]


def test_schedule_times(templates):
    schedules = templates["orchestration"].find_resources("AWS::Scheduler::Schedule")
    expressions = {
        props["Properties"]["Name"]: props["Properties"]["ScheduleExpression"]
        for props in schedules.values()
    }
    assert expressions == {
        "premium-entry-run": "cron(0 10 ? * MON-FRI *)",
        "premium-monitor-run": "rate(5 minutes)",
        "premium-closer-run": "cron(45 15 ? * MON-FRI *)",
        "premium-reconciler-run": "cron(15 16 ? * MON-FRI *)",
    }


def test_executors_receive_their_own_scan_result(templates):
    machines = templates["orchestration"].find_resources("AWS::StepFunctions::StateMachine")
    definitions = [str(m["Properties"]["DefinitionString"]) for m in machines.values()]
    entry = next(d for d in definitions if "ScanIC" in d)
    assert '"Payload.$":"$.scanIC.Payload"' in entry
    assert '"Payload.$":"$.scanCS.Payload"' in entry
    assert '"Variable":"$.scanIC.Payload.should_trade"' in entry


def test_secret_grant_matches_the_strategy_configuration():
    from stacks.compute_stack import BROKER_SECRET_NAME

    from common.config import CS_SPY_CONFIG, IC_SPY_CONFIG

    assert IC_SPY_CONFIG.secret_name == CS_SPY_CONFIG.secret_name == BROKER_SECRET_NAME


def test_every_function_reads_only_the_broker_secret(templates):
    for function_name in _function_ids(templates["compute"]):
        grants = _granted(templates["compute"], function_name)
        secret_grants = {kind for action, kind in grants if action.startswith("secretsmanager")}
        assert secret_grants == {"BrokerSecret"}, function_name


def test_scanner_cannot_change_positions_or_trades(templates):
    grants = _granted(templates["compute"], "premium-scanner")
    assert ("dynamodb:Query", "PositionsTable") in grants
    assert ("dynamodb:PutItem", "MarketStateTable") in grants
    assert not {kind for _, kind in grants} & {"TradesTable", "DataBucket", "TradeAlerts"}
    assert ("dynamodb:PutItem", "PositionsTable") not in grants


def test_reconciler_reads_trading_state_and_writes_only_the_summary(templates):
    grants = _granted(templates["compute"], "premium-reconciler")
    writes = {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"}
    tables = {"PositionsTable", "TradesTable", "MarketStateTable"}
    assert not {(a, k) for a, k in grants if a in writes and k in tables}
    assert ("s3:PutObject", "DataBucket") in grants
    assert ("s3:DeleteObject*", "DataBucket") not in grants


def test_trading_functions_write_positions_and_trades(templates):
    for function_name in ["premium-executor", "premium-monitor", "premium-closer"]:
        grants = _granted(templates["compute"], function_name)
        assert ("dynamodb:PutItem", "PositionsTable") in grants
        assert ("dynamodb:PutItem", "TradesTable") in grants
        assert "MarketStateTable" not in {kind for _, kind in grants}
        assert "DataBucket" not in {kind for _, kind in grants}


def test_metric_publishing_is_limited_to_the_dashboard_namespace(templates):
    policies = templates["compute"].find_resources("AWS::IAM::Policy")
    statements = [
        s
        for policy in policies.values()
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
        if s["Action"] == "cloudwatch:PutMetricData"
    ]
    assert len(statements) == 4
    assert all(
        s["Condition"] == {"StringEquals": {"cloudwatch:namespace": "Premium/Trading"}}
        for s in statements
    )


def test_summary_versions_expire(templates):
    bucket = next(iter(templates["storage"].find_resources("AWS::S3::Bucket").values()))
    rules = {r["Id"]: r for r in bucket["Properties"]["LifecycleConfiguration"]["Rules"]}
    summaries = rules["ExpireSummaries"]
    assert summaries["ExpirationInDays"] == 365
    assert summaries["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30
    assert "Transitions" not in summaries
    assert rules["RemoveExpiredDeleteMarkers"]["ExpiredObjectDeleteMarker"] is True
