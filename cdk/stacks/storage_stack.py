"""S3 bucket and DynamoDB tables for premium-selling data."""

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_s3 as s3,
)
from constructs import Construct


class StorageStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # S3 bucket for daily summaries
        self.bucket = s3.Bucket(
            self,
            "DataBucket",
            bucket_name=f"premium-selling-data-{self.account}",
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            # Summaries are a few KB. S3 does not transition objects under 128 KB to
            # Infrequent Access by default, so they stay in Standard until expiry.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireSummaries",
                    prefix="daily-summaries/",
                    expiration=Duration.days(365),
                    noncurrent_version_expiration=Duration.days(30),
                ),
                s3.LifecycleRule(
                    id="RemoveExpiredDeleteMarkers",
                    prefix="daily-summaries/",
                    expired_object_delete_marker=True,
                ),
            ],
        )

        # DynamoDB: Positions table (strategy + spread_id)
        self.positions_table = dynamodb.Table(
            self,
            "PositionsTable",
            table_name="premium-positions",
            partition_key=dynamodb.Attribute(
                name="strategy",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="spread_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # DynamoDB: Trades table (strategy + trade_id)
        self.trades_table = dynamodb.Table(
            self,
            "TradesTable",
            table_name="premium-trades",
            partition_key=dynamodb.Attribute(
                name="strategy",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="trade_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # DynamoDB: Market State table (date + timestamp)
        self.market_state_table = dynamodb.Table(
            self,
            "MarketStateTable",
            table_name="premium-market-state",
            partition_key=dynamodb.Attribute(
                name="date",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            time_to_live_attribute="ttl_epoch",
        )
