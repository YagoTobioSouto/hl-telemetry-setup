"""Similarity stack: zip-packaged Lambda that computes TF-IDF + ROUGE-L.

Stateless service. The Lambda receives a candidate email draft plus a
list of reference emails and returns per-reference similarity scores
(plus a summary block that picks out the closest match). Invoked
directly via the IAM-authed ``lambda:InvokeFunction`` API —
``InvocationType=Event`` for fire-and-forget, or ``RequestResponse``
for synchronous calls during dev/testing.

Packaging: zip (not container image).
    - Handler module: ``lambda/handler.py``
    - Dependencies: ``lambda/requirements.txt`` (scikit-learn + rouge-score)
    - Bundling: CDK shells out to the public AWS Lambda Python 3.12 image
      to ``pip install -r requirements.txt -t /asset-output`` so the
      compiled wheels (numpy, scipy) match the Lambda runtime's glibc.

Sizing rationale (after swapping BERTScore out for TF-IDF):
    - Memory: 512 MB is plenty. sklearn's TfidfVectorizer on short
      emails is tiny; numpy + scipy idle at ~80 MB. Headroom for larger
      reference sets without re-tuning.
    - Timeout: 10s. Warm invocations are ~50 ms; cold starts land
      around 1s. 10s is defensive margin.
    - No provisioned concurrency — cold starts are already sub-second.
    - No VPC — nothing to reach inside a VPC from this handler.

No environment variables: behaviour is hard-coded in the handler to
keep configuration and deployment footprints minimal.
"""

from __future__ import annotations

import os

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    DockerImage,
    Duration,
    Stack,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct


# --- Packaging constants ---------------------------------------------------
# Pin to the Lambda Python 3.12 base image so numpy / scipy wheels install
# against the same glibc and CPU arch the Lambda will actually run on.
# Using the public ECR repository avoids Docker Hub rate-limit headaches.
_BUNDLING_IMAGE = "public.ecr.aws/sam/build-python3.12:latest"

# Bundling command: install deps into /asset-output, then copy the source
# alongside them. /asset-output becomes the zip contents.
_BUNDLING_COMMAND = (
    "pip install --no-cache-dir -r requirements.txt -t /asset-output "
    "&& cp -au handler.py /asset-output/"
)


class SimilarityStack(Stack):
    """TF-IDF + ROUGE-L similarity Lambda (zip-packaged)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Asset root is the sibling ``lambda/`` directory at the repo root.
        lambda_asset_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "lambda"
        )

        fn = _lambda.Function(
            self,
            "SimilarityHandler",
            function_name="copycraft-similarity-handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(
                lambda_asset_path,
                bundling=BundlingOptions(
                    image=DockerImage.from_registry(_BUNDLING_IMAGE),
                    command=["bash", "-c", _BUNDLING_COMMAND],
                ),
            ),
            # 512 MB is far more than we need — sklearn on short texts
            # peaks around ~150 MB. Leaves headroom for larger reference
            # sets without re-tuning.
            memory_size=512,
            # Warm invocations are ~50 ms; cold starts ~1s. 10s is
            # defensive margin without wasting money on long timeouts.
            timeout=Duration.seconds(10),
            log_retention=logs.RetentionDays.ONE_MONTH,
            description=(
                "TF-IDF + ROUGE-L email similarity scoring. Zip-packaged, "
                "stateless, no model, no VPC. Informational only."
            ),
        )

        # --- Outputs -------------------------------------------------------
        CfnOutput(self, "FunctionName", value=fn.function_name)
        CfnOutput(self, "FunctionArn", value=fn.function_arn)
