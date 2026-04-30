#!/usr/bin/env python3
"""CDK entry point for the BERTScore similarity stack.

Pinned to eu-west-2 to align with the rest of the Copycraft deployment.
Account is resolved from CDK_DEFAULT_ACCOUNT which the CDK CLI injects
at synth/deploy time.
"""

import os

import aws_cdk as cdk

from stacks import SimilarityStack

app = cdk.App()

_env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region="eu-west-2",
)

SimilarityStack(app, "CopycraftSimilarity", env=_env)

app.synth()
