#!/usr/bin/env python3
"""CDK entry point for the telemetry stack."""

import aws_cdk as cdk

from stacks import TelemetryStack

app = cdk.App()
TelemetryStack(app, "TelemetryStack")
app.synth()
