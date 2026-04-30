"""Wrapper around the similarity scoring Lambda.

Plain Python function — NOT a Strands tool. The evaluator agent no
longer delegates the decision-of-when-to-call to the LLM; we call
the Lambda ourselves in code and hand the result to the LLM as part
of the prompt. That fixes two problems at once:

1. Nemotron (and probably other open models) has documented tool-call
   reliability issues — "tool-call failures that break the execution
   loop" is one of the two dominant failure modes NVIDIA flags in its
   own Nemotron 3 Super docs. Combining tools with structured output
   in a single agent call produced 30+ tool invocations per run.

2. There's no agent-loop reason for the Lambda call to be a tool. The
   LLM isn't choosing between tools, isn't sequencing, isn't deciding
   whether to call. It just needs the scoring output. Making that an
   LLM decision is complexity for its own sake.

Two execution modes for the Lambda call:

* ``mode="local"``: import ``lambda/handler.py`` and run it in-process.
  No AWS creds, no network. Used for fast iteration.
* ``mode="live"``: SigV4-authed ``lambda:InvokeFunction`` against the
  deployed handler. Production shape.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Literal

import boto3

from config import LAMBDA_FUNCTION_NAME, LAMBDA_REGION

_LOG = logging.getLogger(__name__)

LambdaMode = Literal["local", "live"]


# --- In-process handler import (cached) -------------------------------

_local_handler: Any = None


def _load_local_handler() -> Any:
    """Import ``lambda/handler.py`` once and cache it."""
    global _local_handler
    if _local_handler is not None:
        return _local_handler

    here = os.path.dirname(os.path.abspath(__file__))
    lambda_src = os.path.normpath(os.path.join(here, "..", "lambda"))
    if lambda_src not in sys.path:
        sys.path.insert(0, lambda_src)

    from handler import lambda_handler  # noqa: E402  pylint: disable=import-outside-toplevel

    _local_handler = lambda_handler
    return _local_handler


# --- Live Lambda client (cached) --------------------------------------

_lambda_client: Any = None


def _get_lambda_client() -> Any:
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=LAMBDA_REGION)
    return _lambda_client


# --- Public entry point -----------------------------------------------


def invoke_similarity_lambda(
    draft_email: str,
    source_emails: list[dict],
    *,
    mode: LambdaMode = "local",
) -> dict:
    """Call the similarity scoring Lambda and return its response body.

    Args:
        draft_email: The final drafted email text.
        source_emails: ``[{"email_id": ..., "text": ...}, ...]``.
        mode: ``"local"`` to import the handler in-process (no AWS),
            ``"live"`` to invoke the deployed Lambda over the network.

    Returns:
        The scoring service's parsed response body, with keys
        ``references`` (ranked list) and ``candidate_summary`` (with
        verdict, confidence, evidence, etc).
    """
    payload = {"candidate": draft_email, "references": source_emails}

    if mode == "live":
        return _invoke_live_lambda(payload)
    return _invoke_local_handler(payload)


def _invoke_local_handler(payload: dict) -> dict:
    """Run the Lambda handler in-process and unwrap its API-Gateway envelope."""
    handler = _load_local_handler()
    response = handler(payload, None)

    status = response.get("statusCode", 500)
    if status != 200:
        _LOG.error("Local handler returned non-200 status: %s", response)
        raise RuntimeError(
            f"Similarity handler failed with status {status}: "
            f"{response.get('body')!r}"
        )
    return json.loads(response["body"])


def _invoke_live_lambda(payload: dict) -> dict:
    """Call the deployed Lambda via SigV4-signed InvokeFunction."""
    client = _get_lambda_client()
    response = client.invoke(
        FunctionName=LAMBDA_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )

    if response.get("FunctionError"):
        raw = response["Payload"].read().decode()
        _LOG.error("Live Lambda function error: %s", raw)
        raise RuntimeError(f"Similarity Lambda raised: {raw}")

    if response.get("StatusCode", 500) >= 300:
        raise RuntimeError(
            f"Live Lambda transport error: status={response.get('StatusCode')}"
        )

    outer = json.loads(response["Payload"].read())
    status = outer.get("statusCode", 500)
    if status != 200:
        raise RuntimeError(
            f"Similarity handler failed with status {status}: "
            f"{outer.get('body')!r}"
        )
    return json.loads(outer["body"])
