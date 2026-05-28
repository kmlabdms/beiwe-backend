#!/usr/bin/env python3
import os

import aws_cdk as cdk

from prerequisites_stack import BeiwePrerequisitesStack

app = cdk.App()

# Region can be supplied via --context region=<r> or CDK_DEFAULT_REGION env var.
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION")
account = os.environ.get("CDK_DEFAULT_ACCOUNT")

BeiwePrerequisitesStack(
    app,
    "BeiwePrerequisitesStack",
    env=cdk.Environment(account=account, region=region),
)

app.synth()
