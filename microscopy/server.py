"""HTTP wrapper around microscopy_analysis.analyze(). Images posted as multipart 'images'."""

import hmac
import os
import tempfile

from cellpose import models
from flask import Flask, jsonify, request
from microscopy_analysis import analyze
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# Required, with no fallback: if this service is deployed to a public endpoint, an
# unauthenticated request would leak results and burn GPU time. Failing to boot
# is the safe default.
TOKEN = os.environ["SERVICE_TOKEN"]

# Loaded once at startup and reused across requests (persistent model).
model = models.CellposeModel(pretrained_model=os.environ["MODEL_PATH"], gpu=True)


@app.errorhandler(Exception)
def as_json(e):
    """Unusable images → a readable message; HTTPException (the 401) passes through."""
    if isinstance(e, HTTPException):
        return e
    return jsonify({"error": f"{type(e).__name__}: {e}"[:500]}), 400


@app.before_request
def require_token():
    # /health is behind the token too, so the service exposes nothing without it.
    sent = request.headers.get("X-Service-Token", "")
    if not hmac.compare_digest(sent, TOKEN):
        return jsonify(error="unauthorized"), 401


@app.get("/health")
def health():
    return "ok"


@app.post("/micro")
def micro_endpoint():
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for f in request.files.getlist("images"):
            p = os.path.join(d, os.path.basename(f.filename))
            f.save(p)
            paths.append(p)
        return jsonify(analyze(paths, model, request.args.get("name", "sample")))
