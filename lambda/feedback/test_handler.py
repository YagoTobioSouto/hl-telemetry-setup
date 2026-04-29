import json
import os
from unittest.mock import MagicMock, patch

os.environ["BUCKET_NAME"] = "test-bucket"


def _make_event(resource: str, body: dict) -> dict:
    """Synthesise an API Gateway proxy event."""
    return {
        "resource": resource,
        "path": resource,
        "httpMethod": "POST",
        "body": json.dumps(body),
        "headers": {"Content-Type": "application/json"},
    }


def test_edit_decision_writes_correct_key():
    with patch("boto3.client") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3
        # Import AFTER patching so the module-level boto3.client picks up the mock
        from handler import lambda_handler

        event = _make_event(
            "/api/feedback/edit-decision",
            {
                "sessionId": "abc-1234",
                "issueId": "iss-001",
                "action": "accept",
                "severity": "Med",
            },
        )
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_s3.put_object.assert_called_once()
        call = mock_s3.put_object.call_args.kwargs
        assert call["Bucket"] == "test-bucket"
        assert call["Key"].startswith("edit-decisions/year=")
        assert call["Key"].endswith("abc-1234_iss-001.json")


def test_rating_writes_correct_key():
    with patch("boto3.client") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3
        from handler import lambda_handler

        event = _make_event(
            "/api/feedback/rating",
            {"sessionId": "abc-1234", "score": 1, "userId": "u-1"},
        )
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        call = mock_s3.put_object.call_args.kwargs
        assert call["Key"].endswith("abc-1234.json")
        assert "ratings/year=" in call["Key"]


def test_missing_session_id_returns_400():
    with patch("boto3.client"):
        from handler import lambda_handler

        event = _make_event("/api/feedback/edit-decision", {})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 400


def test_unknown_resource_returns_400():
    with patch("boto3.client"):
        from handler import lambda_handler

        event = _make_event("/api/feedback/bogus", {"sessionId": "x"})
        result = lambda_handler(event, None)
        assert result["statusCode"] == 400
