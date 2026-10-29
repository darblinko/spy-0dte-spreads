"""Lambda functions (Container Image) for premium-selling."""

from aws_cdk import (
    Duration,
    Stack,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from constructs import Construct

from .observability_stack import ObservabilityStack
from .storage_stack import StorageStack


class ComputeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        storage: StorageStack,
        observability: ObservabilityStack,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        # Shared environment variables
        common_env = {
            "S3_BUCKET": storage.bucket.bucket_name,
            "POSITIONS_TABLE": storage.positions_table.table_name,
            "TRADES_TABLE": storage.trades_table.table_name,
            "MARKET_STATE_TABLE": storage.market_state_table.table_name,
            "SNS_TRADE_TOPIC": observability.trade_topic.topic_arn,
            "SNS_ERROR_TOPIC": observability.error_topic.topic_arn,
        }

        src_path = "../src"

        # --- Scanner Lambda ---
        self.scanner_fn = _lambda.DockerImageFunction(
            self,
            "ScannerFn",
            function_name="premium-scanner",
            code=_lambda.DockerImageCode.from_image_asset(
                src_path,
                file="Dockerfile",
                cmd=["scanner.handler.handler"],
            ),
            memory_size=1024,
            timeout=Duration.seconds(120),
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
        )

        # --- Executor Lambda ---
        self.executor_fn = _lambda.DockerImageFunction(
            self,
            "ExecutorFn",
            function_name="premium-executor",
            code=_lambda.DockerImageCode.from_image_asset(
                src_path,
                file="Dockerfile",
                cmd=["executor.handler.handler"],
            ),
            memory_size=512,
            timeout=Duration.seconds(120),
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
        )

        # --- Monitor Lambda ---
        self.monitor_fn = _lambda.DockerImageFunction(
            self,
            "MonitorFn",
            function_name="premium-monitor",
            code=_lambda.DockerImageCode.from_image_asset(
                src_path,
                file="Dockerfile",
                cmd=["monitor.handler.handler"],
            ),
            memory_size=512,
            timeout=Duration.seconds(60),
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
        )

        # --- Closer Lambda ---
        self.closer_fn = _lambda.DockerImageFunction(
            self,
            "CloserFn",
            function_name="premium-closer",
            code=_lambda.DockerImageCode.from_image_asset(
                src_path,
                file="Dockerfile",
                cmd=["closer.handler.handler"],
            ),
            memory_size=512,
            timeout=Duration.seconds(120),
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
        )

        # --- Reconciler Lambda ---
        self.reconciler_fn = _lambda.DockerImageFunction(
            self,
            "ReconcilerFn",
            function_name="premium-reconciler",
            code=_lambda.DockerImageCode.from_image_asset(
                src_path,
                file="Dockerfile",
                cmd=["reconciler.handler.handler"],
            ),
            memory_size=512,
            timeout=Duration.seconds(120),
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
        )

        # Grant permissions
        all_fns = [
            self.scanner_fn,
            self.executor_fn,
            self.monitor_fn,
            self.closer_fn,
            self.reconciler_fn,
        ]

        for fn in all_fns:
            storage.positions_table.grant_read_write_data(fn)
            storage.trades_table.grant_read_write_data(fn)
            storage.market_state_table.grant_read_write_data(fn)
            storage.bucket.grant_read_write(fn)
            observability.trade_topic.grant_publish(fn)
            observability.error_topic.grant_publish(fn)

            # Secrets Manager access for Alpaca API credentials
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:premium/*",
                    ],
                )
            )
