"""HTTP wrapper around fcs_count.count(). Body is the raw .fcs bytes; od/uL in query."""

import hmac
import os
import tempfile

from fcs_count import count
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# Required, with no fallback: if this service is deployed to a public endpoint, an
# unauthenticated request would leak results and burn compute. Failing to boot is
# the safe default. /health is behind it too, so the service exposes nothing
# without the token.
TOKEN = os.environ["SERVICE_TOKEN"]


@app.before_request
def require_token():
    sent = request.headers.get("X-Service-Token", "")
    if not hmac.compare_digest(sent, TOKEN):
        return jsonify(error="unauthorized"), 401


@app.errorhandler(Exception)
def as_json(e):
    """Bad .fcs → a readable message, not a 500 HTML page the portal can't parse."""
    if isinstance(e, HTTPException):
        return e
    return jsonify({"error": f"{type(e).__name__}: {e}"[:500]}), 400


@app.get("/health")
def health():
    return "ok"


@app.post("/count")
def count_endpoint():
    od = request.args["od"]
    uL = request.args["uL"]
    sample_id = request.args.get("name")
    with tempfile.NamedTemporaryFile(suffix=".fcs") as f:
        f.write(request.data)
        f.flush()
        return jsonify(count(f.name, od, uL, sample_id=sample_id))
