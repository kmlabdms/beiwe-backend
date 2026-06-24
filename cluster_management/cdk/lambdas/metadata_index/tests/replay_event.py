"""In-process replay harness for metadata-index events (U5).

Runs a fixture event through the real handler against a moto-mocked DynamoDB
table and reports the resulting index state. This is for LOCAL development only
-- there is no live `aws lambda invoke` / SQS-send mode. The deployed-stack smoke
check lives in the U6 runbook (cluster_management/cdk/METADATA_INDEX.md).

    python replay_event.py fixtures/mixed_batch.json
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import boto3
from moto import mock_aws

# Make the Lambda package importable when run as a script (mirrors conftest).
_PKG_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

import handler  # noqa: E402

TABLE_NAME = "replay-upload-metadata-index"


def _create_table(resource):
    table = resource.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                   {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    return table


def replay(event: dict):
    """Replay an SQS event through the handler; return (handler_result, items)."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = _create_table(resource)
        os.environ["TABLE_NAME"] = TABLE_NAME
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        handler._TABLE = None
        try:
            result = handler.handler(event, None)
            items = table.scan()["Items"]
        finally:
            handler._TABLE = None
            os.environ.pop("TABLE_NAME", None)
        return result, items


def _coerce(value):
    # moto returns numbers as Decimal; render them as ints for readability.
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    event = json.loads(pathlib.Path(argv[0]).read_text())
    result, items = replay(event)
    print("batchItemFailures:", result["batchItemFailures"])
    print(f"index items ({len(items)}):")
    for item in sorted(items, key=lambda i: (i["PK"], i["SK"])):
        rest = {k: _coerce(v) for k, v in item.items() if k not in ("PK", "SK")}
        print(f"  {item['PK']} | {item['SK']} | {rest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
