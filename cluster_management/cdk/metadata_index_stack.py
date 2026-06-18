"""Upload Metadata Index — additive, event-driven S3 upload monitoring layer.

A self-contained stack (U1, U2, U4-stack of the plan):

  raw S3 bucket --(ObjectCreated)--> EventBridge --> SQS (+DLQ) --> Lambda --> DynamoDB

It NEVER mutates the Beiwe upload path. The only bucket-side change is enabling
the EventBridge-notifications flag via CDK's managed Custom::S3BucketNotifications
resource, which merges (not clobbers) and reverts on `cdk destroy`.

Disable: delete/disable the EventBridge rule. Teardown: `cdk destroy MetadataIndexStack`.
Both leave the bucket's object data and Beiwe untouched.
"""
import os

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
)
from constructs import Construct

_LAMBDA_ASSET = os.path.join(os.path.dirname(__file__), "lambdas", "metadata_index")
# Files in the lambda dir that must not ship in the deployment bundle.
_ASSET_EXCLUDE = ["tests", "tests/*", "__pycache__", "*.pyc", "*.md", ".pytest_cache"]


def is_truthy(value) -> bool:
    """Interpret a CDK context value as a boolean opt-in flag.

    Context values arrive as strings (`-c enable_metadata_index=true`), so a plain
    `if value:` would treat the string "false" as enabled. Only explicit
    affirmatives enable the stack.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class MetadataIndexStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        raw_bucket_name: str,
        ttl_days: int = 90,
        metric_namespace: str = "BeiweUploadMetadata",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- U1: DynamoDB single-table index + least-privilege reader role -----
        table = dynamodb.Table(
            self,
            "Table",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",          # used only by per-object dedupe records
            removal_policy=RemovalPolicy.RETAIN,    # operational-safety, NOT a retention policy
        )

        # Read access for downstream consumers (e.g. Grafana). Least privilege:
        # Query + GetItem only, no Scan, no writes. Assumable by named principals
        # the account grants sts:AssumeRole to — not the AdministratorAccess
        # deploy user. The table is a full participant-upload roster.
        reader_role = iam.Role(
            self,
            "ReaderRole",
            assumed_by=iam.AccountRootPrincipal(),
            description="Least-privilege read access to the upload metadata index.",
        )
        table.grant(reader_role, "dynamodb:Query", "dynamodb:GetItem")

        # --- U2: SQS buffer + DLQ ---------------------------------------------
        dlq = sqs.Queue(self, "Dlq", retention_period=Duration.days(14))
        queue = sqs.Queue(
            self,
            "Queue",
            visibility_timeout=Duration.seconds(180),  # >= 6x the Lambda timeout
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=dlq),
        )

        # --- U2: wire the existing (unmanaged) raw bucket's events to the queue
        # Import by name — never create or take ownership of the bucket.
        raw_bucket = s3.Bucket.from_bucket_name(self, "RawBucket", raw_bucket_name)
        # Native call: emits the managed Custom::S3BucketNotifications resource,
        # which reverts the flag on destroy. No hand-written custom resource.
        raw_bucket.enable_event_bridge_notification()

        rule = events.Rule(
            self,
            "ObjectCreatedRule",
            description=f"Route {raw_bucket_name} ObjectCreated events to the metadata index queue",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [raw_bucket_name]}},
            ),
        )
        # targets.SqsQueue adds the queue resource policy granting events.amazonaws.com
        # SendMessage with an aws:SourceArn condition pinned to this rule (confused-
        # deputy guard). The parser remains the authoritative prefix/shape filter.
        rule.add_target(targets.SqsQueue(queue))

        # --- U4-stack: writer Lambda + IAM (no s3:*) + SQS event source --------
        log_group = logs.LogGroup(
            self,
            "WriterLogs",
            retention=logs.RetentionDays.ONE_MONTH,  # bound PII-in-logs retention
            removal_policy=RemovalPolicy.DESTROY,
        )
        writer_fn = lambda_.Function(
            self,
            "WriterFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(_LAMBDA_ASSET, exclude=_ASSET_EXCLUDE),
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=log_group,
            environment={
                "TABLE_NAME": table.table_name,
                "TTL_DAYS": str(ttl_days),
                "METRIC_NAMESPACE": metric_namespace,
            },
        )
        # Write-only on the table (the handler never reads). No s3 grant at all —
        # this is the IAM enforcement of "we never read raw contents" (R2).
        table.grant_write_data(writer_fn)
        writer_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": metric_namespace}},
            )
        )
        writer_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                queue,
                batch_size=10,
                max_batching_window=Duration.seconds(20),
                report_batch_item_failures=True,
            )
        )

        # --- Outputs (for the Lambda env, future Grafana wiring, ops) ----------
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "TableArn", value=table.table_arn)
        CfnOutput(self, "ReaderRoleArn", value=reader_role.role_arn)
        CfnOutput(self, "QueueUrl", value=queue.queue_url)
        CfnOutput(self, "DlqUrl", value=dlq.queue_url)
        CfnOutput(self, "WriterFunctionName", value=writer_fn.function_name)
