"""Structural validation of the n8n workflow files + execution of their Code nodes under Node with n8n-style mocks.

What this proves: the JSON parses, the graph is wired, nodes/credentials are well-formed, no secrets are embedded, and
the JavaScript in every Code node parses and behaves as intended on representative input.
What it does NOT prove: that n8n accepts the import or that Google Drive OAuth works - that needs a real n8n.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

N8N = Path(__file__).resolve().parents[2] / "n8n"
FILES = ["mis_discover_files.json", "mis_process_file.json", "mis_reconcile_batch.json", "mis_approve_mapping.json"]
KNOWN = {"n8n-nodes-base.scheduleTrigger": {1.2}, "n8n-nodes-base.manualTrigger": {1}, "n8n-nodes-base.httpRequest": {4.2},
         "n8n-nodes-base.code": {2}, "n8n-nodes-base.if": {2}, "n8n-nodes-base.webhook": {2},
         "n8n-nodes-base.respondToWebhook": {1.1}, "n8n-nodes-base.stickyNote": {1}}
TRIGGERS = {"n8n-nodes-base.scheduleTrigger", "n8n-nodes-base.manualTrigger", "n8n-nodes-base.webhook"}
HAS_NODE = shutil.which("node") is not None


def load(fn):
    return json.loads((N8N / fn).read_text(encoding="utf-8"))


class Structure(unittest.TestCase):
    def test_four_required_workflows_exist_with_expected_names(self):
        names = {load(f)["name"] for f in FILES}
        self.assertTrue({"MIS - Discover Files", "MIS - Process File", "MIS - Reconcile Batch"} <= names)

    def test_each_workflow_is_well_formed(self):
        for fn in FILES:
            wf = load(fn)
            nodes = wf["nodes"]
            names = [n["name"] for n in nodes]
            ids = [n["id"] for n in nodes]
            self.assertEqual(len(names), len(set(names)), fn)
            self.assertEqual(len(ids), len(set(ids)), fn)
            self.assertFalse(wf["active"], fn)
            self.assertEqual(wf["settings"]["timezone"], "Asia/Kolkata")
            for n in nodes:
                self.assertIn(n["type"], KNOWN, f"{fn}: {n['type']}")
                self.assertIn(n["typeVersion"], KNOWN[n["type"]], f"{fn}: {n['name']}")
                self.assertEqual(len(n["position"]), 2)
            triggers = [n for n in nodes if n["type"] in TRIGGERS]
            self.assertGreaterEqual(len(triggers), 1, fn)
            by = set(names)
            for src, outs in wf["connections"].items():
                self.assertIn(src, by, f"{fn}: connection from unknown node {src}")
                for branch in outs["main"]:
                    for c in branch:
                        self.assertIn(c["node"], by, f"{fn}: {src} -> unknown {c['node']}")
            # every non-trigger, non-note node is reachable from a trigger
            reach, stack = set(), [t["name"] for t in triggers]
            while stack:
                x = stack.pop()
                if x in reach:
                    continue
                reach.add(x)
                for branch in wf["connections"].get(x, {}).get("main", []):
                    stack += [c["node"] for c in branch]
            for n in nodes:
                if n["type"] != "n8n-nodes-base.stickyNote":
                    self.assertIn(n["name"], reach, f"{fn}: {n['name']} is not reachable from a trigger")

    def test_if_nodes_have_two_outputs_wired_as_designed(self):
        for fn in FILES:
            wf = load(fn)
            for n in wf["nodes"]:
                if n["type"] == "n8n-nodes-base.if":
                    outs = wf["connections"].get(n["name"], {}).get("main", [])
                    self.assertGreaterEqual(len(outs), 1, f"{fn}: {n['name']}")
                    self.assertTrue(outs[0], f"{fn}: {n['name']} true branch unconnected")

    def test_credentials_are_references_never_secrets(self):
        allowed = {"httpHeaderAuth", "googleDriveOAuth2Api"}
        text = ""
        for fn in FILES:
            raw = (N8N / fn).read_text(encoding="utf-8")
            text += raw
            for n in load(fn)["nodes"]:
                for k, v in (n.get("credentials") or {}).items():
                    self.assertIn(k, allowed)
                    self.assertEqual(set(v), {"id", "name"})
        for pat in (r"sk-ant-", r"AIza[0-9A-Za-z_-]{20,}", r"ya29\.", r"[0-9a-f]{32,}", r"postgres(ql)?://\w+:\w+@", r"Bearer\s+[A-Za-z0-9]{16,}"):
            self.assertIsNone(re.search(pat, text), pat)

    def test_no_hard_coded_hosts_folders_or_emails(self):
        for fn in FILES:
            raw = (N8N / fn).read_text(encoding="utf-8")
            self.assertNotIn("localhost", raw)
            self.assertIsNone(re.search(r"[\w.]+@[\w.]+\.\w+", raw))
            for n in load(fn)["nodes"]:
                if n["type"] == "n8n-nodes-base.httpRequest":
                    url = n["parameters"]["url"]
                    self.assertTrue(url.startswith("={{") or url.startswith("https://www.googleapis.com/"), f"{fn}: {n['name']}: {url}")

    def test_processor_calls_use_env_url_and_api_credential(self):
        for fn in FILES:
            for n in load(fn)["nodes"]:
                if n["type"] != "n8n-nodes-base.httpRequest":
                    continue
                url = n["parameters"]["url"]
                if "MIS_PROCESSOR_URL" in url:
                    self.assertEqual(n["credentials"]["httpHeaderAuth"]["name"], "MIS Processor API Key", f"{fn}: {n['name']}")
                if "N8N_INTERNAL_URL" in url:
                    self.assertEqual(n["credentials"]["httpHeaderAuth"]["name"], "MIS Webhook Secret")

    def test_webhooks_require_authentication(self):
        for fn in FILES:
            for n in load(fn)["nodes"]:
                if n["type"] == "n8n-nodes-base.webhook":
                    self.assertEqual(n["parameters"]["authentication"], "headerAuth")
                    self.assertTrue(n.get("credentials"))

    def test_expressions_balanced(self):
        for fn in FILES:
            raw = (N8N / fn).read_text(encoding="utf-8")
            for s in re.findall(r'"(=[^"]*)"', raw):
                self.assertEqual(s.count("{{"), s.count("}}"), s)

    def test_discover_uses_createdtime_paging_and_overlap_window(self):
        wf = load("mis_discover_files.json")
        js = "\n".join(n["parameters"].get("jsCode", "") for n in wf["nodes"])
        self.assertIn("createdTime >", js)
        self.assertIn("pageToken", js)
        self.assertIn("md5Checksum", js)
        urls = [n["parameters"]["url"] for n in wf["nodes"] if n["type"] == "n8n-nodes-base.httpRequest"]
        self.assertTrue(any("/v1/discovery/window" in u for u in urls))
        self.assertTrue(any("/v1/discovery/complete" in u for u in urls))


@unittest.skipUnless(HAS_NODE, "node not installed")
class CodeNodes(unittest.TestCase):
    HARNESS = r"""
const fs = require('fs');
const {code, input, env, nodes} = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const mkItem = (j, b) => ({ json: j, binary: b });
const $input = { first: () => mkItem(input[0]), all: () => input.map((j) => mkItem(j)), last: () => mkItem(input[input.length - 1]) };
const $ = (name) => {
  const n = nodes[name];
  if (!n) throw new Error(`Node '${name}' hasn't been executed`);
  return { first: () => mkItem(n[0], n.binary), last: () => mkItem(n[n.length - 1]), all: () => n.map((j) => mkItem(j)), item: mkItem(n[0]) };
};
const $env = env;
const fn = new Function('$input', '$', '$env', `return (async () => {${code}})();`);
fn($input, $, $env).then((r) => process.stdout.write(JSON.stringify(r)))
  .catch((e) => { process.stdout.write(JSON.stringify({ __error: e.message })); });
"""

    def run_node(self, wf_file, node_name, *, input, env=None, nodes=None):
        wf = load(wf_file)
        js = next(n["parameters"]["jsCode"] for n in wf["nodes"] if n["name"] == node_name)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "h.js").write_text(self.HARNESS)
            (Path(d) / "in.json").write_text(json.dumps({"code": js, "input": input, "env": env or {}, "nodes": nodes or {}}))
            out = subprocess.run(["node", str(Path(d) / "h.js"), str(Path(d) / "in.json")], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_every_code_node_parses(self):
        for fn in FILES:
            for n in load(fn)["nodes"]:
                if n["type"] == "n8n-nodes-base.code":
                    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                        f.write("(async () => {\n" + n["parameters"]["jsCode"] + "\n})();")
                    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"{fn}/{n['name']}: {r.stderr}")

    def test_init_paging_requires_folder_and_window(self):
        w = {"created_after": "2026-10-17T00:00:00Z", "scan_started_at": "2026-10-20T06:00:00+00:00", "mode": "rolling_lookback"}
        r = self.run_node("mis_discover_files.json", "Init Paging", input=[w], env={})
        self.assertIn("MIS_DRIVE_FOLDER_ID", r["__error"])
        r = self.run_node("mis_discover_files.json", "Init Paging", input=[w], env={"MIS_DRIVE_FOLDER_ID": "F1"})
        self.assertEqual(r[0]["json"]["folder_id"], "F1")

    def test_build_list_url_first_page_and_next_page(self):
        init = {"created_after": "2026-10-17T00:00:00Z", "folder_id": "FOLDER'1"}
        first = self.run_node("mis_discover_files.json", "Build List URL", input=[init], nodes={"Init Paging": [init]})
        url = first[0]["json"]["url"]
        self.assertTrue(url.startswith("https://www.googleapis.com/drive/v3/files?"))
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["q"][0], "'FOLDER\\'1' in parents and trashed = false and createdTime > '2026-10-17T00:00:00Z'")
        self.assertNotIn("pageToken", q)
        self.assertEqual((q["orderBy"], q["pageSize"], q["supportsAllDrives"]), (["createdTime"], ["100"], ["true"]))
        self.assertIn("md5Checksum", q["fields"][0])
        nxt = self.run_node("mis_discover_files.json", "Build List URL", input=[{}],
                            nodes={"Init Paging": [init], "List Drive Files": [{"nextPageToken": "TOK123"}]})
        self.assertEqual(parse_qs(urlparse(nxt[0]["json"]["url"]).query)["pageToken"], ["TOK123"])

    def test_split_events_and_summarize(self):
        ev = {"events": [{"event_id": 1, "drive_file_id": "a", "filename": "x.csv", "status": "discovered"}]}
        r = self.run_node("mis_discover_files.json", "Split Events", input=[ev])
        self.assertEqual(r, [{"json": {"event_id": 1, "drive_file_id": "a", "filename": "x.csv"}}])
        r = self.run_node("mis_discover_files.json", "Split Events", input=[{"events": []}])
        self.assertEqual(r, [])
        s = self.run_node("mis_discover_files.json", "Summarize", input=[{"outcome": "processed"}, {"outcome": "processed"}, {"outcome": "needs_mapping"}, {"error": "x"}])
        self.assertEqual(s[0]["json"], {"total": 4, "counts": {"processed": 2, "needs_mapping": 1, "error": 1}, "attention": 2})

    def test_process_validate_input_rejects_bad_ids(self):
        ok = self.run_node("mis_process_file.json", "Validate Input", input=[{"body": {"event_id": 5, "drive_file_id": "1AbC_d-9", "filename": "f.csv"}}])
        self.assertEqual(ok[0]["json"]["event_id"], 5)
        for bad in ({"event_id": 0, "drive_file_id": "a"}, {"event_id": "x", "drive_file_id": "a"},
                    {"event_id": 3, "drive_file_id": "a b; rm"}, {"event_id": 3}):
            self.assertIn("__error", self.run_node("mis_process_file.json", "Validate Input", input=[{"body": bad}]))

    def test_attention_message_only_when_something_needs_a_person(self):
        self.assertEqual(self.run_node("mis_discover_files.json", "Attention Message", input=[{"count": 0, "text": ""}]), [])
        r = self.run_node("mis_discover_files.json", "Attention Message", input=[{"count": 1, "text": "[MIS] 1 file(s) need attention:\n- x.csv: needs_review"}])
        self.assertIn("x.csv", r[0]["json"]["text"])

    def test_compose_notification_never_includes_row_data(self):
        prop = {"notification": "Mapping proposal #4 needs a decision."}
        r = self.run_node("mis_process_file.json", "Compose Notification", input=[prop],
                          nodes={"Attach File Again": [{"event_id": 7, "filename": "kb.csv", "outcome": "needs_mapping"}]})
        self.assertIn("proposal #4", r[0]["json"]["text"])
        err = self.run_node("mis_process_file.json", "Compose Notification", input=[{"error": "bad_request"}],
                            nodes={"Attach File Again": [{"event_id": 7, "filename": "kb.csv", "outcome": "needs_mapping"}]})
        self.assertIn("could not be created", err[0]["json"]["text"])

    def test_reconcile_selects_only_wanted_retries_and_announces_only_changes(self):
        pend = {"events": [{"event_id": 1, "drive_file_id": "a", "filename": "a.csv"}, {"event_id": 2, "drive_file_id": "b", "filename": "b.csv"}]}
        r = self.run_node("mis_reconcile_batch.json", "Select Retries", input=[pend], nodes={"Reconcile (check)": [{"retry_event_ids": [2]}]})
        self.assertEqual([x["json"]["event_id"] for x in r], [2])
        out = {"batches": [{"date": "2026-10-19", "status": "incomplete", "notify": True, "summary_hash": "h1", "message": "m"},
                           {"date": "2026-10-18", "status": "complete", "notify": False, "summary_hash": "h0", "message": "m0"}]}
        r = self.run_node("mis_reconcile_batch.json", "Batches To Announce", input=[out])
        self.assertEqual([x["json"]["date"] for x in r], ["2026-10-19"])

    def test_approve_validate_requires_named_approver(self):
        self.assertIn("__error", self.run_node("mis_approve_mapping.json", "Validate Input", input=[{"body": {"profile_id": 3}}]))
        ok = self.run_node("mis_approve_mapping.json", "Validate Input", input=[{"body": {"profile_id": 3, "approved_by": " Asha ", "accept_flags": ["x"]}}])
        self.assertEqual(ok[0]["json"], {"profile_id": 3, "approved_by": "Asha", "accept_flags": ["x"]})


if __name__ == "__main__":
    unittest.main()
