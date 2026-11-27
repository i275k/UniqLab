"""Print the JSON report written by `UniqLab --self-test` (used in CI logs). Exits 1 if it failed or is missing."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: str) -> int:
    report = Path(path)
    if not report.is_file():
        print(f"self-test report not found: {path}")
        return 1
    data = json.loads(report.read_text(encoding="utf-8"))
    for check in data["checks"]:
        mark = "PASS" if check["ok"] else "FAIL"
        detail = json.dumps(check["detail"], ensure_ascii=False) if not isinstance(check["detail"], str) else check["detail"]
        print(f"{mark}  {check['name']} ({check['seconds']} s): {detail[:600]}")
        if not check["ok"] and check.get("trace"):
            print(check["trace"])
    print("self-test:", "passed" if data["ok"] else "FAILED")
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
