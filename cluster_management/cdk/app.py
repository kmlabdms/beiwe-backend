#!/usr/bin/env python3
import os

import aws_cdk as cdk

from prerequisites_stack import BeiwePrerequisitesStack
from scheduler_stack import BeiweSchedulerStack

app = cdk.App()

# Region can be supplied via --context region=<r> or CDK_DEFAULT_REGION env var.
region   = app.node.try_get_context("region")   or os.environ.get("CDK_DEFAULT_REGION")
account  = os.environ.get("CDK_DEFAULT_ACCOUNT")
env_name = app.node.try_get_context("env_name") or "kowalski-beiwe"

env = cdk.Environment(account=account, region=region)

BeiwePrerequisitesStack(app, "BeiwePrerequisitesStack", env=env)

BeiweSchedulerStack(app, "BeiweSchedulerStack", env_name=env_name, env=env)

app.synth()
