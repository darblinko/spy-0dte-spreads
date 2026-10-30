"""Lambda functions (Container Image) for premium-selling."""

from pathlib import Path

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
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from .observability_stack import ObservabilityStack
from .storage_stack import StorageStack

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
# Matches StrategyConfig.secret_name in src/common/config.py.
BROKER_SECRET_NAME = "premium/spy-api"
METRIC_NAMESPACE = "Premium/Trading"


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

        src_path = str(SRC_DIR)

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

        self._grant_least_privilege(storage, observability)

    def _grant_least_privilege(
        self, storage: StorageStack, observability: ObservabilityStack
    ) -> None:
        """Grant each function only the tables, topics and secret its handler uses."""
        broker_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "BrokerSecret", BROKER_SECRET_NAME
        )
        trading_fns = [self.executor_fn, self.monitor_fn, self.closer_fn]
        all_fns = [self.scanner_fn, *trading_fns, self.reconciler_fn]

        for fn in all_fns:
            broker_secret.grant_read(fn)
            observability.error_topic.grant_publish(fn)

        storage.positions_table.grant_read_data(self.scanner_fn)
        storage.market_state_table.grant_write_data(self.scanner_fn)

        for fn in trading_fns:
            storage.positions_table.grant_read_write_data(fn)
            storage.trades_table.grant_read_write_data(fn)
            observability.trade_topic.grant_publish(fn)

        storage.positions_table.grant_read_data(self.reconciler_fn)
        storage.trades_table.grant_read_data(self.reconciler_fn)
        storage.market_state_table.grant_read_data(self.reconciler_fn)
        storage.bucket.grant_put(self.reconciler_fn)
        observability.trade_topic.grant_publish(self.reconciler_fn)

        metric_policy = iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},
        )
        for fn in [self.scanner_fn, self.monitor_fn, self.closer_fn, self.reconciler_fn]:
            fn.add_to_role_policy(metric_policy)
