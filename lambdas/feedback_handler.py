import json
import os
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")
BUCKET = os.environ["BUCKET_NAME"]


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        resource = event.get("resource", "")
        now = datetime.now(timezone.utc)
        partition = f"year={now.year}/month={now.month:02d}/day={now.day:02d}"

        if resource == "/api/feedback/edit-decision":
            session_id = body["sessionId"]
            issue_id = body["issueId"]
            # TODO: PII redaction on issueText, suggestionText via Comprehend
            body["timestamp"] = now.isoformat()
            key = f"edit-decisions/{partition}/{session_id}_{issue_id}.json"

        elif resource == "/api/feedback/rating":
            session_id = body["sessionId"]
            # TODO: PII redaction on comment via Comprehend
            body["timestamp"] = now.isoformat()
            key = f"ratings/{partition}/{session_id}.json"

        else:
            return _response(400, {"error": f"Unknown resource: {resource}"})

        s3.put_object(
            Bucket=BUCKET,
            Key=key,
            Body=json.dumps(body),
            ContentType="application/json",
        )
        return _response(200, {"message": "ok", "key": key})

    except KeyError as e:
        return _response(400, {"error": f"Missing field: {e}"})
    except Exception as e:
        return _response(500, {"error": str(e)})


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
