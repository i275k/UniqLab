"""The packaged-build self-test also runs from source (without downloading tools)."""

from __future__ import annotations

import json


def test_selftest_without_tools(qapp, tmp_path, english):
    from uniqlab import selftest

    report = tmp_path / "report.json"
    code = selftest.run(str(report))
    data = json.loads(report.read_text(encoding="utf-8"))
    failed = [check for check in data["checks"] if not check["ok"]]
    assert code == 0 and data["ok"], failed
    names = {check["name"] for check in data["checks"]}
    assert {"resources", "languages", "language_switching", "photo_processing_pillow"} <= names
