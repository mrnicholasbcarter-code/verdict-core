"""Run an isolated ocr review the way verdict/orchestration/review.py does. Never prints the key.

Called by review_pr.sh (run it through ~/.config/verdict/with-new-gateway-key.sh so the gateway key is
current). Arguments: repo base head model out background. OCR_BIN overrides the ocr executable.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from verdict.credentials_store import resolve_gateway_key

repo, base, head, model, out, background = sys.argv[1:7]
resolved = resolve_gateway_key()
if resolved is None:
    sys.exit("no gateway key")
ocr_bin = os.environ.get("OCR_BIN") or str(Path.home() / ".local" / "bin" / "ocr")
home = Path(tempfile.mkdtemp(prefix="ocr-home-"))
try:
    cfg = home / ".opencodereview"
    cfg.mkdir()
    provider = {"url": "http://127.0.0.1:20128/v1", "protocol": "openai", "api_key": resolved[0]}
    (cfg / "config.json").write_text(
        json.dumps({"custom_providers": {"verdict-omniroute": provider}, "llm": {}})
    )
    env = dict(os.environ, HOME=str(home))
    argv = [
        ocr_bin,
        "review",
        "--repo",
        repo,
        "--from",
        base,
        "--to",
        head,
        "--format",
        "json",
        "--audience",
        "agent",
        "--effort",
        os.environ.get("OCR_EFFORT", "low"),
        "--max-tokens-budget",
        os.environ.get("OCR_BUDGET", "400000"),
        "--provider",
        "verdict-omniroute",
        "--model",
        model,
        "--background",
        background,
        "--output",
        out,
    ]
    if os.environ.get("OCR_RULE"):
        argv += ["--rule", os.environ["OCR_RULE"]]
    argv += ["--max-tokens", os.environ.get("OCR_MAX_TOKENS", "200000")]
    if os.environ.get("OCR_CONCURRENCY"):
        argv += ["--concurrency", os.environ["OCR_CONCURRENCY"]]
    r = subprocess.run(
        argv,
        env=env,
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("OCR_SUBPROCESS_TIMEOUT", "2300")),
    )
    print("exit", r.returncode)
    print(r.stderr[-1500:])
finally:
    shutil.rmtree(home, ignore_errors=True)
