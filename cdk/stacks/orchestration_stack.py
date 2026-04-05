"""Step Functions workflow and EventBridge Scheduler for premium-selling.

Workflow:
1. Scanner (10:00 AM) -> detects regime, returns scan result
2. If should_trade: Executor -> builds spread, submits orders
3. Monitor loop every 5 min (9:35 AM - 3:44 PM) -> checks P&L thresholds
4. Closer (3:45 PM) -> force close all positions
5. Reconciler (4:15 PM) -> daily summary to S3
"""

from aws_cdk import (
    Stack, Duration,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
    aws_scheduler as scheduler,
    aws_iam as iam,
)
from constructs import Construct

from .compute_stack import ComputeStack
from .observability_stack import ObservabilityStack


class OrchestrationStack(Stack):
    def __init__(self, scope: Construct, id: str, *,
                 compute: ComputeStack,
                 observability: ObservabilityStack,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # --- Step Function Tasks ---

        # Scanner: detect market regime for IC and CS strategies
        scan_ic = sfn_tasks.LambdaInvoke(
            self, "ScanIC",
            lambda_function=compute.scanner_fn,
            payload=sfn.TaskInput.from_object({"strategy": "IC_SPY"}),
            result_path="$.scanIC",
            result_selector={
                "Payload.$": "$.Payload",
            },
            retry_on_service_exceptions=True,
        )
        scan_ic.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2,
        )

        scan_cs = sfn_tasks.LambdaInvoke(
            self, "ScanCS",
            lambda_function=compute.scanner_fn,
            payload=sfn.TaskInput.from_object({"strategy": "CS_SPY"}),
            result_path="$.scanCS",
            result_selector={
                "Payload.$": "$.Payload",
            },
            retry_on_service_exceptions=True,
        )
        scan_cs.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(30),
            backoff_rate=2,
        )

        # Executor: build and submit spreads
        execute_ic = sfn_tasks.LambdaInvoke(
            self, "ExecuteIC",
            lambda_function=compute.executor_fn,
            payload=sfn.TaskInput.from_json_path_at("$.scanIC.Payload"),
            result_path="$.executeIC",
            retry_on_service_exceptions=True,
        )
        execute_ic.add_retry(
            errors=["States.ALL"],
            max_attempts=1,
            interval=Duration.seconds(10),
        )

        execute_cs = sfn_tasks.LambdaInvoke(
            self, "ExecuteCS",
            lambda_function=compute.executor_fn,
            payload=sfn.TaskInput.from_json_path_at("$.scanCS.Payload"),
            result_path="$.executeCS",
            retry_on_service_exceptions=True,
        )
        execute_cs.add_retry(
            errors=["States.ALL"],
            max_attempts=1,
            interval=Duration.seconds(10),
        )

        # Conditional: only execute if scanner says should_trade
        check_ic_trade = sfn.Choice(self, "ShouldTradeIC")
        check_cs_trade = sfn.Choice(self, "ShouldTradeCS")

        skip_ic = sfn.Pass(self, "SkipIC")
        skip_cs = sfn.Pass(self, "SkipCS")

        ic_branch = check_ic_trade.when(
            sfn.Condition.boolean_equals("$.scanIC.Payload.should_trade", True),
            execute_ic,
        ).otherwise(skip_ic)

        cs_branch = check_cs_trade.when(
            sfn.Condition.boolean_equals("$.scanCS.Payload.should_trade", True),
            execute_cs,
        ).otherwise(skip_cs)

        # Scan parallel, then conditionally execute
        scan_parallel = sfn.Parallel(self, "ScanParallel",
                                     result_path="$.scanResults")
        scan_parallel.branch(scan_ic.next(ic_branch))
        scan_parallel.branch(scan_cs.next(cs_branch))

        # --- Entry Workflow: scan + execute ---
        self.entry_workflow = sfn.StateMachine(
            self, "EntryWorkflow",
            state_machine_name="premium-entry-workflow",
            definition_body=sfn.DefinitionBody.from_chainable(scan_parallel),
            timeout=Duration.minutes(15),
        )

        # --- Monitor Workflow: check positions ---
        monitor_task = sfn_tasks.LambdaInvoke(
            self, "MonitorPositions",
            lambda_function=compute.monitor_fn,
            result_path="$.monitorResult",
            retry_on_service_exceptions=True,
        )
        monitor_task.add_retry(
            errors=["States.ALL"],
            max_attempts=2,
            interval=Duration.seconds(10),
        )

        self.monitor_workflow = sfn.StateMachine(
            self, "MonitorWorkflow",
            state_machine_name="premium-monitor-workflow",
            definition_body=sfn.DefinitionBody.from_chainable(monitor_task),
            timeout=Duration.minutes(5),
        )

        # --- EventBridge Scheduler ---
        scheduler_role = iam.Role(
            self, "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.entry_workflow.grant_start_execution(scheduler_role)
        self.monitor_workflow.grant_start_execution(scheduler_role)
        compute.closer_fn.grant_invoke(scheduler_role)
        compute.reconciler_fn.grant_invoke(scheduler_role)

        # Entry: 10:00 AM ET -- scan market + execute trades
        scheduler.CfnSchedule(
            self, "EntrySchedule",
            schedule_expression="cron(0 10 ? * MON-FRI *)",
            schedule_expression_timezone="America/New_York",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.entry_workflow.state_machine_arn,
                role_arn=scheduler_role.role_arn,
            ),
            name="premium-entry-run",
            state="ENABLED",
        )

        # Monitor: every 5 min from 10:05 AM to 3:40 PM ET (67 invocations)
        # Cron can't do "every 5 min within a window", so use rate-based
        # with a Step Functions guard. For now: run every 5 min, the monitor
        # Lambda itself is idempotent and cheap when no positions exist.
        scheduler.CfnSchedule(
            self, "MonitorSchedule",
            schedule_expression="rate(5 minutes)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.monitor_workflow.state_machine_arn,
                role_arn=scheduler_role.role_arn,
            ),
            name="premium-monitor-run",
            state="ENABLED",
        )

        # Closer: 3:45 PM ET -- force close all positions
        scheduler.CfnSchedule(
            self, "CloserSchedule",
            schedule_expression="cron(45 15 ? * MON-FRI *)",
            schedule_expression_timezone="America/New_York",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=compute.closer_fn.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
            name="premium-closer-run",
            state="ENABLED",
        )

        # Reconciler: 4:15 PM ET -- daily summary
        scheduler.CfnSchedule(
            self, "ReconcilerSchedule",
            schedule_expression="cron(15 16 ? * MON-FRI *)",
            schedule_expression_timezone="America/New_York",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF",
            ),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=compute.reconciler_fn.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
            name="premium-reconciler-run",
            state="ENABLED",
        )
