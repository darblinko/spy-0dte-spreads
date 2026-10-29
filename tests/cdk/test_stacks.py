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
