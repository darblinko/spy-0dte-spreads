#!/usr/bin/env python3
"""CDK app entry point for premium-selling system."""

import aws_cdk as cdk

from stacks.storage_stack import StorageStack
from stacks.observability_stack import ObservabilityStack
from stacks.compute_stack import ComputeStack
from stacks.orchestration_stack import OrchestrationStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-east-1",
)

storage = StorageStack(app, "PremiumStorage", env=env)
observability = ObservabilityStack(app, "PremiumObservability", env=env)

compute = ComputeStack(
    app, "PremiumCompute",
    storage=storage,
    observability=observability,
    env=env,
)

OrchestrationStack(
    app, "PremiumOrchestration",
    compute=compute,
    observability=observability,
    env=env,
)

app.synth()
