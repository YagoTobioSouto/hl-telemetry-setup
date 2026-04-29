"""Telemetry stack: S3 bucket for feedback data, Lambda handler, REST API Gateway.

Phase 1 infrastructure for the telemetry pipeline. Supports both
/api/feedback/edit-decision and /api/feedback/rating routes via a single Lambda
that dispatches on `event["resource"]`.
"""

from __future__ import annotations

import os
from typing import Optional

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigateway,
    aws_cognito as cognito,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct


class TelemetryStack(Stack):
    """Telemetry infrastructure stack.

    Args:
        scope: Parent construct.
        construct_id: Stack logical ID.
        user_pool: Optional Cognito user pool. When provided, both feedback
            routes require a valid Cognito JWT via `CognitoUserPoolsAuthorizer`.
            When ``None`` (dev default), routes are open — do not deploy this
            configuration to production.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        user_pool: Optional[cognito.IUserPool] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- S3 bucket for feedback data -----------------------------------
        # Encryption is omitted intentionally — S3 applies SSE-S3 by default
        # to all new buckets. Contracts.md §5 specifies KMS_MANAGED; the
        # deviation is tracked in contract-conflicts.md §13.
        bucket = s3.Bucket(
            self,
            "TelemetryBucket",
            bucket_name=f"copycraft-telemetry-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            # TODO(contract-conflict): flip to RETAIN for prod; kept as DESTROY
            # for dev iteration. See contract-conflicts.md §2.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="edit-decisions-retention",
                    prefix="edit-decisions/",
                    expiration=Duration.days(365),
                ),
                s3.LifecycleRule(
                    id="ratings-retention",
                    prefix="ratings/",
                    expiration=Duration.days(365),
                ),
            ],
        )

        # --- Single Lambda for both feedback routes ------------------------
        lambda_asset_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "lambda", "feedback"
        )
        fn = _lambda.Function(
            self,
            "FeedbackHandler",
            function_name="copycraft-feedback-handler",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(lambda_asset_path),
            environment={"TELEMETRY_BUCKET": bucket.bucket_name},
            timeout=Duration.seconds(10),
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # S3 PutObject on the telemetry bucket
        bucket.grant_put(fn)

        # Comprehend PII detection. Scoped to * because DetectPiiEntities
        # operates on arbitrary text, not a specific resource.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["comprehend:DetectPiiEntities"],
                resources=["*"],
            )
        )

        # --- REST API ------------------------------------------------------
        api = apigateway.RestApi(
            self,
            "TelemetryApi",
            rest_api_name="copycraft-feedback",
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=[
                    "Content-Type",
                    "X-Amz-Date",
                    "Authorization",
                    "X-Api-Key",
                    "X-Amz-Security-Token",
                    "X-Session-Id",
                    "X-Trace-Id",
                ],
            ),
        )

        integration = apigateway.LambdaIntegration(fn)

        # Build the optional Cognito authorizer. When user_pool is not
        # provided, routes are open (dev). See contract-conflicts.md §11.
        method_options: dict = {}
        if user_pool is not None:
            authorizer = apigateway.CognitoUserPoolsAuthorizer(
                self,
                "FeedbackAuthorizer",
                cognito_user_pools=[user_pool],
            )
            method_options = {
                "authorizer": authorizer,
                "authorization_type": apigateway.AuthorizationType.COGNITO,
            }

        api_resource = api.root.add_resource("api")
        # /api/generate and /api/personalise are placeholders for Phase 2
        # when they will proxy to AgentCore Runtime (AGUI).
        api_resource.add_resource("generate")
        api_resource.add_resource("personalise")

        feedback = api_resource.add_resource("feedback")
        feedback.add_resource("edit-decision").add_method(
            "POST", integration, **method_options
        )
        feedback.add_resource("rating").add_method(
            "POST", integration, **method_options
        )

        # --- Outputs -------------------------------------------------------
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
