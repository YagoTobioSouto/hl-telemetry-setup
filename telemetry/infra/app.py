#!/usr/bin/env python3
"""CDK entry point for the telemetry stack.

Pins the deployment to ``eu-west-2`` to satisfy the contract's
Comprehend-access dependency (see ``contracts.md`` → Dependencies). The
account is resolved from ``CDK_DEFAULT_ACCOUNT`` which the CDK CLI
injects at synth/deploy time.

When integrated into the combined Copycraft ``app.py``, replace this file
and wire in ``user_pool=auth_stack.user_pool`` on the stack constructor.
"""

import os

import aws_cdk as cdk

from stacks import TelemetryStack

app = cdk.App()

_env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region="eu-west-2",
)

TelemetryStack(app, "CopycraftTelemetry", env=_env)

app.synth()
