"""SNS topics and CloudWatch dashboard for premium-selling."""

from aws_cdk import (
    Duration,
    Stack,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_sns as sns,
)
from constructs import Construct


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # SNS Topics
        self.trade_topic = sns.Topic(
            self,
            "TradeAlerts",
            topic_name="premium-trade-alerts",
            display_name="Premium Trade Alerts",
        )

        self.error_topic = sns.Topic(
            self,
            "ErrorAlerts",
            topic_name="premium-error-alerts",
            display_name="Premium Error Alerts",
        )

        # CloudWatch Dashboard
        self.dashboard = cloudwatch.Dashboard(
            self,
            "TradingDashboard",
            dashboard_name="Premium-Selling-Dashboard",
        )

        self.dashboard.add_widgets(
            cloudwatch.TextWidget(
                markdown="# Premium Selling System\n0DTE Iron Condor & Credit Spread Dashboard",
                width=24,
                height=2,
            ),
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Daily P&L",
                left=[
                    cloudwatch.Metric(
                        namespace="Premium/Trading",
                        metric_name="DailyPnL",
                        statistic="Sum",
                        period=Duration.days(1),
                    ),
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Active Positions",
                left=[
                    cloudwatch.Metric(
                        namespace="Premium/Trading",
                        metric_name="ActivePositions",
                        dimensions_map={"Strategy": "IC_SPY"},
                        statistic="Maximum",
                        period=Duration.minutes(5),
                    ),
                    cloudwatch.Metric(
                        namespace="Premium/Trading",
                        metric_name="ActivePositions",
                        dimensions_map={"Strategy": "CS_SPY"},
                        statistic="Maximum",
                        period=Duration.minutes(5),
                    ),
                ],
                width=12,
            ),
        )

        self.dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Market Regime",
                left=[
                    cloudwatch.Metric(
                        namespace="Premium/Trading",
                        metric_name="RegimeConfidence",
                        statistic="Average",
                        period=Duration.hours(1),
                    ),
                ],
                width=12,
            ),
            cloudwatch.GraphWidget(
                title="Trade Win Rate",
                left=[
                    cloudwatch.Metric(
                        namespace="Premium/Trading",
                        metric_name="WinRate",
                        statistic="Average",
                        period=Duration.days(1),
                    ),
                ],
                width=12,
            ),
        )
