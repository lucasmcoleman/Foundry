"""Render the actual UI JavaScript with hostile config and artifact names."""
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = '\'"><img src=x onerror=alert(1)><input value="'


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def render_js(body):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute frontend regressions")
    html = (ROOT / "ui" / "index.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    script = script[:script.rfind("(async () => {")]
    harness = """
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const listing = {innerHTML: '', querySelectorAll: () => []};
const context = {console, setTimeout: () => 0, clearTimeout: () => {},
  document: {getElementById: () => listing}, window: {},
  localStorage: {setItem: () => {}, getItem: () => null},
  listing, payload: input.payload};
vm.createContext(context);
vm.runInContext(input.script, context);
Promise.resolve(vm.runInContext(input.body, context)).then(value => console.log(JSON.stringify(value)));
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "script": script, "body": body, "payload": PAYLOAD,
    }), capture_output=True, text=True, check=True, timeout=10)
    return json.loads(result.stdout)


def test_saved_workflow_values_remain_data_in_all_stage_panels():
    panels = render_js("""
S.enabledStages = new Set();
for (const config of Object.values(S.config)) {
  for (const key of Object.keys(config)) {
    if (typeof config[key] === 'string') config[key] = payload;
  }
}
S.config.training.datasets = [payload];
S.config.upload.repo_id_auto = false;
[renderTraining(), renderExport(), renderHeretic(), renderReap(), renderQAT(), renderMagicQuant(), renderROCmFPX(), renderUpload()];
""")
    for panel in panels:
        parsed = Tags(panel)
        assert not any(tag == "img" for tag, _ in parsed.tags)
        assert not any("onerror" in attrs for _, attrs in parsed.tags)
    assert any(attrs.get("value") == PAYLOAD for _, attrs in Tags(panels[0]).tags)


def test_history_names_cannot_add_markup_or_executable_handlers():
    markup = render_js("""
authFetch = async () => ({json: async () => ({runs: [{
  model: payload, ggufs: [{name: payload, size_gb: 1}],
  logs: [{name: payload, modified: 0, size: 1}]
}]})});
(async () => { await loadRunHistory(); return listing.innerHTML; })();
""")
    tags = Tags(markup).tags
    assert not any(tag == "img" for tag, _ in tags)
    for _, attrs in tags:
        assert "onerror" not in attrs
        if "history-gguf" == attrs.get("class") or "history-log-entry" == attrs.get("class"):
            assert "onclick" not in attrs
    assert "&lt;img" in markup


def test_new_upload_defaults_are_private_but_saved_optin_survives():
    result = render_js("""
const initial = S.config.upload.upload_dataset;
localStorage.getItem = () => JSON.stringify({version: LS_VERSION, config: {upload: {upload_dataset: true}}});
restoreFormState();
({initial, saved: S.config.upload.upload_dataset});
""")
    assert result == {"initial": False, "saved": True}
