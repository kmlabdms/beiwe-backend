"""CDK synthesis tests for MetadataIndexStack (U1 / U2 / U4-stack).

Asserts the synthesized CloudFormation has the right resources and, crucially,
the security properties the review required: the writer Lambda role has NO s3:*
action (the IAM enforcement of "never read raw contents", R2), the reader role
is Query/GetItem-only (no Scan), the imported bucket is never created, and the
SQS policy carries an aws:SourceArn confused-deputy guard.
"""
import pathlib
import sys

# Make the CDK dir importable (metadata_index_stack lives there).
_CDK_DIR = pathlib.Path(__file__).resolve().parents[3]
if str(_CDK_DIR) not in sys.path:
    sys.path.insert(0, str(_CDK_DIR))

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from metadata_index_stack import MetadataIndexStack, is_truthy


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = MetadataIndexStack(app, "TestMetaStack", raw_bucket_name="my-raw-bucket")
    return Template.from_stack(stack)


def _iam_policies(template):
    return template.find_resources("AWS::IAM::Policy")


def _statement_actions(statement):
    actions = statement.get("Action", [])
    return actions if isinstance(actions, list) else [actions]


# --- U1: DynamoDB table + reader role ----------------------------------------

def test_single_dynamodb_table_on_demand_ttl_retain(template):
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.has_resource_properties("AWS::DynamoDB::Table", {
        "BillingMode": "PAY_PER_REQUEST",
        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
    })
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})


def test_reader_role_is_query_getitem_only_no_scan(template):
    reader_policies = [
        body for body in _iam_policies(template).values()
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]
        if any(a.startswith("dynamodb:Query") for a in _statement_actions(stmt))
    ]
    assert reader_policies, "expected a reader policy granting dynamodb:Query"
    all_reader_actions = {
        a
        for body in reader_policies
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]
        for a in _statement_actions(stmt)
        if a.startswith("dynamodb:")
    }
    assert "dynamodb:Query" in all_reader_actions
    assert "dynamodb:GetItem" in all_reader_actions
    assert "dynamodb:Scan" not in all_reader_actions


def test_reader_role_trust_scoped_to_principal_when_provided():
    """With reader_principal_arn set, the reader role trusts that exact principal
    (not the whole account), so the participant roster isn't assumable account-wide."""
    app = cdk.App()
    stack = MetadataIndexStack(
        app, "ScopedMetaStack", raw_bucket_name="my-raw-bucket",
        reader_principal_arn="arn:aws:iam::123456789012:user/beiwe-web",
    )
    tmpl = Template.from_stack(stack)
    tmpl.has_resource_properties("AWS::IAM::Role", {
        "Description": "Least-privilege read access to the upload metadata index.",
        "AssumeRolePolicyDocument": {
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": "sts:AssumeRole",
                    "Principal": {"AWS": "arn:aws:iam::123456789012:user/beiwe-web"},
                }),
            ]),
        },
    })


# --- U2: SQS + DLQ + EventBridge wiring on the imported bucket ----------------

def test_two_queues_with_redrive(template):
    template.resource_count_is("AWS::SQS::Queue", 2)  # main + DLQ
    template.has_resource_properties("AWS::SQS::Queue", {"RedrivePolicy": Match.any_value()})


def test_eventbridge_rule_for_object_created(template):
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {"source": ["aws.s3"], "detail-type": ["Object Created"]},
    })


def test_managed_bucket_notification_resource_present(template):
    # enable_event_bridge_notification() emits CDK's managed custom resource.
    template.resource_count_is("Custom::S3BucketNotifications", 1)


def test_imported_bucket_is_never_created(template):
    template.resource_count_is("AWS::S3::Bucket", 0)


def test_sqs_policy_has_source_arn_condition(template):
    policies = template.find_resources("AWS::SQS::QueuePolicy")
    found = False
    for body in policies.values():
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            if "aws:SourceArn" in str(stmt.get("Condition", {})):
                found = True
    assert found, "SQS queue policy must pin aws:SourceArn (confused-deputy guard)"


# --- U4-stack: Lambda, event source, and the no-s3 IAM guarantee -------------

def test_writer_lambda_runtime_and_handler(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "Runtime": "python3.12",
        "Handler": "handler.handler",
    })


def test_event_source_mapping_reports_batch_item_failures(template):
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
    })


def test_writer_role_has_no_s3_actions(template):
    """The writer's own policy (identified by its dynamodb write grant) must not
    contain any s3: action. (CDK's bucket-notifications handler legitimately uses
    s3: actions in a *separate* role — this targets the writer specifically.)"""
    writer_policies = [
        body for body in _iam_policies(template).values()
        if any(
            a.startswith("dynamodb:PutItem")
            for stmt in body["Properties"]["PolicyDocument"]["Statement"]
            for a in _statement_actions(stmt)
        )
    ]
    assert writer_policies, "expected a writer policy granting dynamodb:PutItem"
    for body in writer_policies:
        for stmt in body["Properties"]["PolicyDocument"]["Statement"]:
            for action in _statement_actions(stmt):
                assert not action.startswith("s3:"), f"writer role must not have {action}"


def test_writer_metrics_grant_is_namespace_scoped(template):
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": "cloudwatch:PutMetricData",
                    "Condition": {"StringEquals": {"cloudwatch:namespace": "BeiweUploadMetadata"}},
                }),
            ]),
        },
    })


# --- app.py opt-in gate logic ------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("false", False), ("0", False), ("no", False),
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True), (True, True),
])
def test_is_truthy_gate(value, expected):
    assert is_truthy(value) is expected
