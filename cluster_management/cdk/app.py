#!/usr/bin/env python3
import os

import aws_cdk as cdk

from prerequisites_stack import BeiwePrerequisitesStack
from scheduler_stack import BeiweSchedulerStack
from metadata_index_stack import MetadataIndexStack, is_truthy

# Default raw-data bucket for the kowalski-beiwe deployment. Override with
# --context raw_bucket_name=<name> for a different environment.
DEFAULT_RAW_BUCKET = "beiwe-data-kowalski-beiwe-rxjfjw9miockp1gzxa42gd2zikzdrd3ndrclz"

app = cdk.App()

# Region can be supplied via --context region=<r> or CDK_DEFAULT_REGION env var.
region   = app.node.try_get_context("region")   or os.environ.get("CDK_DEFAULT_REGION")
account  = os.environ.get("CDK_DEFAULT_ACCOUNT")
env_name = app.node.try_get_context("env_name") or "kowalski-beiwe"

env = cdk.Environment(account=account, region=region)

BeiwePrerequisitesStack(app, "BeiwePrerequisitesStack", env=env)

BeiweSchedulerStack(app, "BeiweSchedulerStack", env_name=env_name, env=env)

# Upload Metadata Index is opt-in and removable (R5): synthesized ONLY when the
# enable_metadata_index context flag is explicitly truthy. This is a new
# conditional-synthesis gate — the other stacks above instantiate unconditionally.
if is_truthy(app.node.try_get_context("enable_metadata_index")):
    raw_bucket_name = app.node.try_get_context("raw_bucket_name") or DEFAULT_RAW_BUCKET
    MetadataIndexStack(app, "MetadataIndexStack", raw_bucket_name=raw_bucket_name, env=env)

app.synth()
