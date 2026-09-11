"""Install a built wheel in isolation and check console imports and the UI."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile


SMOKE = r'''
from importlib.metadata import distribution
from pathlib import Path
import os
import sys
import asyncio

site = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(site))
# Check console imports BEFORE ui.app adds core/ to sys.path: a source-only
# test setup used to hide broken package imports in foundry-upload.
for entry in distribution("foundry").entry_points:
    if entry.group == "console_scripts":
        assert callable(entry.load()), entry.name
import core.pipeline
assert Path(core.pipeline.__file__).resolve().is_relative_to(site)
# Exercise a lazy console path too, before UI sys.path setup can hide it.
config = core.pipeline.PipelineConfig(
    upload=core.pipeline.UploadConfig(repo_id="review/model", license="mit")
)
upload_config = core.pipeline._build_hf_upload_config(config, lambda *a: None)
assert upload_config.repo_id == "review/model"
assert not upload_config.upload_dataset
os.environ["FOUNDRY_CONFIG_PATH"] = str(Path.cwd() / "ui-config.json")
os.environ.pop("FOUNDRY_REQUIRE_AUTH", None)
os.environ.pop("FOUNDRY_WORK_DIR", None)
os.environ.pop("FOUNDRY_OUTPUT_DIR", None)
from fastapi.testclient import TestClient
import ui.app
assert Path(ui.app.__file__).resolve().is_relative_to(site)
assert ui.app.FOUNDRY_DIR == Path.cwd()
ui.app._assert_output_dir_contained("./output")
assert asyncio.run(ui.app.run_script("print('installed worker passed')", "./output")) == 0
logs = list((Path.cwd() / "output").glob("_stage_*.log"))
assert len(logs) == 1 and "installed worker passed" in logs[0].read_text()
assert not (site / "output").exists(), "jobs must not write into site-packages"
with TestClient(ui.app.app) as client:
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert "<html" in response.text.lower()
    assert client.get("/health").status_code == 200
assert not (site / "ui" / "config.json").exists(), "operator config must not ship"
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="foundry-wheel-") as directory:
        site = Path(directory) / "site"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
             "--target", str(site), str(wheel)], check=True,
        )
        subprocess.run(
            [sys.executable, "-I", "-c", SMOKE, str(site)],
            cwd=directory, check=True,
        )
    print("Wheel console imports, UI page, health route, and installed worker passed.")


if __name__ == "__main__":
    main()
