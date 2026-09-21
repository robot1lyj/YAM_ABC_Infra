"""Exercise the actual browser error translator without starting hardware."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_operator_hints_preserve_diagnostics_and_give_recovery():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    source = Path("yam_abc_reproduce/hil/static/app.js").read_text()
    function = source.split("function operatorHint(message) {", 1)[1].split("function toast", 1)[0]
    messages = ["RTC committed target changed at actuation", "SDK state update stale",
                "CAN interface(s) not up", "episode queue full", "invalid policy response",
                "Replay refused: replay start pose differs", "No space left", "Failed to fetch",
                "unexpected backend failure", "已保持"]
    script = "function operatorHint(message) {" + function
    script += "console.log(JSON.stringify(" + json.dumps(messages) + ".map(operatorHint)));"
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    hints = json.loads(result.stdout)
    for raw, hint in zip(messages[:-1], hints[:-1]):
        assert raw in hint and "原始诊断" in hint
    assert "若Follower已在零位" in hints[0] and "Leader回零" in hints[0]
    assert hints[-1] == "已保持"
