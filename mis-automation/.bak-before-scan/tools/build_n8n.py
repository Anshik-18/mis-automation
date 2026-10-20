#!/usr/bin/env python3
"""Generates the importable n8n workflow JSON files in n8n/.

The workflows are authored here (not by hand) so ids/connections stay consistent and are re-buildable:
    python3 tools/build_n8n.py
They use only core nodes (Schedule/Webhook/HTTP Request/Code/IF/Respond) and talk to Google Drive through its REST
API with n8n's own Google Drive OAuth2 credential. No secret appears in the JSON - credentials are references.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "n8n"
NS = uuid.UUID("6f1c2a3e-0b7d-4c1e-9a55-3f2d6b1e0a10")

CRED_API = {"httpHeaderAuth": {"id": "mis-processor-key", "name": "MIS Processor API Key"}}
CRED_HOOK = {"httpHeaderAuth": {"id": "mis-webhook-secret", "name": "MIS Webhook Secret"}}
CRED_SMTP = {"smtp": {"id": "mis-smtp", "name": "MIS SMTP"}}
CRED_DRIVE = {"googleDriveOAuth2Api": {"id": "mis-google-drive", "name": "MIS Google Drive"}}

P = "$env.MIS_PROCESSOR_URL"
HOOK = "$env.N8N_INTERNAL_URL"


class WF:
    def __init__(self, name: str, timezone: str = "Asia/Kolkata"):
        self.name, self.tz = name, timezone
        self.nodes: list[dict] = []
        self.conns: dict = {}

    def add(self, name, type_, version, params, pos, *, creds=None, **extra):
        n = {"parameters": params, "id": str(uuid.uuid5(NS, self.name + "/" + name)), "name": name, "type": type_,
             "typeVersion": version, "position": list(pos)}
        if creds:
            n["credentials"] = creds
        n.update(extra)
        self.nodes.append(n)
        return name

    def link(self, src, dst, out=0):
        outs = self.conns.setdefault(src, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": dst, "type": "main", "index": 0})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.link(a, b)

    def export(self):
        return {"name": self.name, "nodes": self.nodes, "connections": self.conns, "active": False,
                "settings": {"executionOrder": "v1", "timezone": self.tz, "saveManualExecutions": True,
                             "saveDataErrorExecution": "all", "saveDataSuccessExecution": "all"},
                "tags": [], "pinData": {}, "meta": {"templateCredsSetupCompleted": False},
                "versionId": str(uuid.uuid5(NS, self.name + "/v1"))}


def code(js: str) -> dict:
    return {"jsCode": js.strip() + "\n"}


def http(w: WF, name, method, url, pos, *, body=None, multipart=None, auth="api", file_response=False,
         never_error=False, on_error=None, timeout=60000, extra_opts=None, once=False, retry=False):
    p: dict = {"method": method, "url": url}
    creds = None
    if auth == "api":
        p.update({"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"})
        creds = CRED_API
    elif auth == "hook":
        p.update({"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"})
        creds = CRED_HOOK
    elif auth == "drive":
        p.update({"authentication": "predefinedCredentialType", "nodeCredentialType": "googleDriveOAuth2Api"})
        creds = CRED_DRIVE
    if body is not None:
        p.update({"sendBody": True, "contentType": "json", "specifyBody": "json", "jsonBody": body})
    if multipart is not None:
        p.update({"sendBody": True, "contentType": "multipart-form-data", "bodyParameters": {"parameters": multipart}})
    opts: dict = {"timeout": timeout}
    resp: dict = {}
    if file_response:
        resp.update({"responseFormat": "file", "outputPropertyName": "data"})
    if never_error:
        resp["neverError"] = True
    if resp:
        opts["response"] = {"response": resp}
    if extra_opts:
        opts.update(extra_opts)
    p["options"] = opts
    extra = {}
    if on_error:
        extra["onError"] = on_error
    if once:
        extra["executeOnce"] = True
    if retry:
        extra.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 3000})
    return w.add(name, "n8n-nodes-base.httpRequest", 4.2, p, pos, creds=creds, **extra)


def if_(w: WF, name, left, op_type, operation, right, pos):
    cond = {"id": str(uuid.uuid5(NS, w.name + "/" + name + "/c")), "leftValue": left,
            "operator": {"type": op_type, "operation": operation}}
    if right is not None:
        cond["rightValue"] = right
    else:
        cond["operator"]["singleValue"] = True          # unary operators (notEmpty, true, ...)
    return w.add(name, "n8n-nodes-base.if", 2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                       "conditions": [cond], "combinator": "and"}, "options": {}}, pos)


def note(w: WF, text, pos, w_=420, h_=180):
    return w.add("Note " + str(len(w.nodes)), "n8n-nodes-base.stickyNote", 1,
                 {"content": text, "height": h_, "width": w_}, pos)


# ====================================================================================================== A. Discover
def discover() -> dict:
    w = WF("MIS - Discover Files")
    w.add("Every Hour", "n8n-nodes-base.scheduleTrigger", 1.2,
          {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}, (0, 300))
    w.add("Run Now (manual)", "n8n-nodes-base.manualTrigger", 1, {}, (0, 480))
    http(w, "Get Scan Window", "GET", f"={{{{ {P} }}}}/v1/discovery/window", (240, 380), retry=True)
    w.add("Init Paging", "n8n-nodes-base.code", 2, code("""
const w = $input.first().json;
const folder = $env.MIS_DRIVE_FOLDER_ID;
if (!folder) throw new Error('MIS_DRIVE_FOLDER_ID is not set');
if (!w.created_after || !w.scan_started_at) throw new Error('processor returned no scan window');
return [{ json: { created_after: w.created_after, scan_started_at: w.scan_started_at, mode: w.mode, folder_id: folder } }];
"""), (480, 380))
    w.add("Build List URL", "n8n-nodes-base.code", 2, code("""
const init = $('Init Paging').first().json;
let token = '';
try { token = $('List Drive Files').last().json.nextPageToken || ''; } catch (e) { token = ''; }
const esc = (s) => String(s).replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'");
const q = `'${esc(init.folder_id)}' in parents and trashed = false and createdTime > '${init.created_after}'`;
const p = new URLSearchParams({
  q, orderBy: 'createdTime', pageSize: '100',
  fields: 'nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,size,md5Checksum,parents)',
  supportsAllDrives: 'true', includeItemsFromAllDrives: 'true',
});
if (token) p.set('pageToken', token);
return [{ json: { url: 'https://www.googleapis.com/drive/v3/files?' + p.toString() } }];
"""), (720, 380))
    http(w, "List Drive Files", "GET", "={{ $json.url }}", (960, 380), auth="drive", retry=True)
    http(w, "Register Files", "POST", f"={{{{ {P} }}}}/v1/discovery/register", (1200, 380),
         body="={{ JSON.stringify({ files: $json.files || [] }) }}", retry=True)
    if_(w, "More Pages?", "={{ $('List Drive Files').last().json.nextPageToken || '' }}", "string", "notEmpty", None, (1440, 380))
    http(w, "Complete Scan", "POST", f"={{{{ {P} }}}}/v1/discovery/complete", (1680, 480),
         body="={{ JSON.stringify({ scan_started_at: $('Init Paging').first().json.scan_started_at }) }}", retry=True)
    http(w, "Get Pending Events", "GET", f"={{{{ {P} }}}}/v1/events/pending?limit=200", (1920, 480), once=True)
    w.add("Split Events", "n8n-nodes-base.code", 2, code("""
const events = $input.first().json.events || [];
return events.map((e) => ({ json: { event_id: e.event_id, drive_file_id: e.drive_file_id, filename: e.filename } }));
"""), (2160, 480))
    http(w, "Process File", "POST", f"={{{{ {HOOK} }}}}/webhook/mis-process-file", (2400, 480), auth="hook",
         body="={{ JSON.stringify({ event_id: $json.event_id, drive_file_id: $json.drive_file_id, filename: $json.filename }) }}",
         never_error=True, timeout=900000)
    w.add("Summarize", "n8n-nodes-base.code", 2, code("""
const rows = $input.all().map((i) => i.json);
const counts = {};
for (const r of rows) { const k = r.outcome || (r.error ? 'error' : 'unknown'); counts[k] = (counts[k] || 0) + 1; }
const attention = rows.filter((r) => ['failed', 'needs_mapping', 'needs_review', 'error'].includes(r.outcome || 'error')).length;
return [{ json: { total: rows.length, counts, attention } }];
"""), (2640, 480))
    w.chain("Every Hour", "Get Scan Window")
    w.chain("Run Now (manual)", "Get Scan Window")
    w.chain("Get Scan Window", "Init Paging", "Build List URL", "List Drive Files", "Register Files", "More Pages?")
    w.link("More Pages?", "Build List URL", 0)          # true: next page
    w.link("More Pages?", "Complete Scan", 1)           # false: done
    w.chain("Complete Scan", "Get Pending Events", "Split Events", "Process File", "Summarize")
    # files that need a person (unknown name, ambiguous, retries exhausted) are announced once per state
    http(w, "Check Attention", "POST", f"={{{{ {P} }}}}/v1/events/attention", (1920, 700), once=True)
    w.add("Attention Message", "n8n-nodes-base.code", 2, code("""
const r = $input.first().json;
return r.count > 0 && r.text ? [{ json: { text: r.text } }] : [];
"""), (2160, 700))
    if_(w, "Notify Configured?", "={{ $env.MIS_NOTIFY_WEBHOOK_URL || '' }}", "string", "notEmpty", None, (2400, 700))
    w.add("Notify Attention", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": "={{ $env.MIS_NOTIFY_WEBHOOK_URL }}", "sendBody": True, "contentType": "json",
        "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ text: $json.text }) }}", "options": {"timeout": 20000}},
        (2640, 620), onError="continueRegularOutput")
    w.link("Complete Scan", "Check Attention")
    w.chain("Check Attention", "Attention Message", "Notify Configured?")
    w.link("Notify Configured?", "Notify Attention", 0)
    note(w, "**Discovery**\nHourly. Asks the processor for the scan window (rolling lookback, or further back after downtime), "
            "lists the Drive folder page by page (createdTime filter), registers every file, then processes whatever is pending.\n"
            "Needs: credentials *MIS Processor API Key*, *MIS Google Drive*, *MIS Webhook Secret*; env MIS_PROCESSOR_URL, "
            "MIS_DRIVE_FOLDER_ID, N8N_INTERNAL_URL. Activate *MIS - Process File* too.", (0, 40), 520, 190)
    return w.export()


# ====================================================================================================== B. Process
def process() -> dict:
    w = WF("MIS - Process File")
    w.add("Webhook", "n8n-nodes-base.webhook", 2, {"httpMethod": "POST", "path": "mis-process-file",
          "authentication": "headerAuth", "responseMode": "responseNode", "options": {}},
          (0, 300), creds=CRED_HOOK, webhookId=str(uuid.uuid5(NS, "webhook/process")))
    w.add("Validate Input", "n8n-nodes-base.code", 2, code("""
const b = $input.first().json.body || {};
const id = Number(b.event_id);
if (!Number.isInteger(id) || id <= 0) throw new Error('event_id must be a positive integer');
if (!b.drive_file_id || !/^[A-Za-z0-9_-]+$/.test(b.drive_file_id)) throw new Error('drive_file_id missing or malformed');
return [{ json: { event_id: id, drive_file_id: b.drive_file_id, filename: String(b.filename || '') } }];
"""), (220, 300))
    http(w, "Claim Event", "POST", f"={{{{ {P} + '/v1/events/' + $json.event_id + '/claim' }}}}", (440, 300),
         never_error=True, retry=False)
    if_(w, "Claimed?", "={{ $json.claim_token || '' }}", "string", "notEmpty", None, (660, 300))
    w.add("Respond - Not Claimable", "n8n-nodes-base.respondToWebhook", 1.1, {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({ outcome: 'skipped_not_claimable', event_id: $('Validate Input').first().json.event_id }) }}",
        "options": {"responseCode": 200}}, (880, 480))
    http(w, "Download From Drive", "GET",
         "={{ 'https://www.googleapis.com/drive/v3/files/' + $('Validate Input').first().json.drive_file_id + '?alt=media&supportsAllDrives=true' }}",
         (880, 300), auth="drive", file_response=True, on_error="continueErrorOutput", timeout=300000)
    http(w, "Report Download Failure", "POST",
         f"={{{{ {P} + '/v1/events/' + $('Validate Input').first().json.event_id + '/fail' }}}}", (1100, 480),
         body="={{ JSON.stringify({ claim_token: $('Claim Event').first().json.claim_token, error: String(($json.error && $json.error.message) || 'download failed').slice(0, 150) }) }}",
         never_error=True)
    w.add("Respond - Download Failed", "n8n-nodes-base.respondToWebhook", 1.1, {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({ outcome: 'failed', stage: 'download', event_id: $('Validate Input').first().json.event_id, event_status: $json.status || null }) }}",
        "options": {"responseCode": 200}}, (1320, 480))
    http(w, "Process In Processor", "POST",
         f"={{{{ {P} + '/v1/events/' + $('Validate Input').first().json.event_id + '/process' }}}}", (1100, 300),
         multipart=[{"name": "claim_token", "value": "={{ $('Claim Event').first().json.claim_token }}"},
                    {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "data"}],
         never_error=True, timeout=600000)
    w.add("Attach File Again", "n8n-nodes-base.code", 2, code("""
// The HTTP node drops the file; put the downloaded bytes back so a mapping proposal can use them.
const res = $input.first().json;
const dl = $('Download From Drive').first();
return [{ json: { ...res, event_id: $('Validate Input').first().json.event_id, filename: $('Validate Input').first().json.filename }, binary: dl.binary }];
"""), (1320, 300))
    if_(w, "Needs Mapping?", "={{ $json.outcome || '' }}", "string", "equals", "needs_mapping", (1540, 300))
    http(w, "Propose Mapping", "POST", f"={{{{ {P} }}}}/v1/mapping/propose", (1760, 200),
         multipart=[{"name": "event_id", "value": "={{ $json.event_id }}"},
                    {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "data"}],
         never_error=True, timeout=300000)
    w.add("Compose Notification", "n8n-nodes-base.code", 2, code("""
// Text for humans. Contains file names, ids and counts only - never customer data.
const j = $input.first().json;
const orig = $('Attach File Again').first().json;
const name = orig.filename || ('event ' + orig.event_id);
let text;
let email = {};
if (j.notification) {
  // the processor already wrote a plain-language change summary (column names and counts only) and a review link
  text = `[MIS] ${name}: new or changed file layout.\n\n${j.review_markdown || j.notification}` + (j.review_url ? `\n\nReview and approve: ${j.review_url}` : '');
  email = { email_subject: j.email_subject, email_text: j.email_text, email_html: j.email_html };
} else if (j.error) {
  text = `[MIS] ${name}: mapping proposal could not be created (${j.error}). A person must submit a mapping.`;
} else {
  text = `[MIS] ${name}: ${orig.outcome || 'unknown outcome'}.`;
}
return [{ json: { text, ...email } }];
"""), (1980, 300))
    if_(w, "Notify Configured?", "={{ $env.MIS_NOTIFY_WEBHOOK_URL || '' }}", "string", "notEmpty", None, (2200, 300))
    w.add("Notify", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": "={{ $env.MIS_NOTIFY_WEBHOOK_URL }}", "sendBody": True, "contentType": "json",
        "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ text: $json.text }) }}", "options": {"timeout": 20000}},
        (2420, 220), onError="continueRegularOutput")
    if_(w, "Email Configured?", "={{ ($('Compose Notification').first().json.email_html && $env.MIS_REVIEW_EMAIL_TO) || '' }}", "string", "notEmpty", None, (2420, 400))
    w.add("Send Review Email", "n8n-nodes-base.emailSend", 2.1, {
        "fromEmail": "={{ $env.MIS_REVIEW_EMAIL_FROM }}", "toEmail": "={{ $env.MIS_REVIEW_EMAIL_TO }}",
        "subject": "={{ $('Compose Notification').first().json.email_subject }}", "emailFormat": "both",
        "text": "={{ $('Compose Notification').first().json.email_text }}",
        "html": "={{ $('Compose Notification').first().json.email_html }}", "options": {}},
        (2640, 520), creds=CRED_SMTP, onError="continueRegularOutput")
    w.add("Respond - Result", "n8n-nodes-base.respondToWebhook", 1.1, {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify($('Attach File Again').first().json) }}",
        "options": {"responseCode": 200}}, (2640, 400))
    w.chain("Webhook", "Validate Input", "Claim Event", "Claimed?")
    w.link("Claimed?", "Download From Drive", 0)
    w.link("Claimed?", "Respond - Not Claimable", 1)
    w.link("Download From Drive", "Process In Processor", 0)
    w.link("Download From Drive", "Report Download Failure", 1)
    w.chain("Report Download Failure", "Respond - Download Failed")
    w.chain("Process In Processor", "Attach File Again", "Needs Mapping?")
    w.link("Needs Mapping?", "Propose Mapping", 0)
    w.link("Needs Mapping?", "Respond - Result", 1)
    w.link("Propose Mapping", "Compose Notification")
    w.chain("Compose Notification", "Notify Configured?")
    w.link("Notify Configured?", "Notify", 0)
    w.link("Notify Configured?", "Email Configured?", 1)
    w.link("Notify", "Email Configured?")
    w.link("Email Configured?", "Send Review Email", 0)
    w.link("Email Configured?", "Respond - Result", 1)
    w.link("Send Review Email", "Respond - Result")
    note(w, "**Process one file** (called by Discover, Reconcile and Approve Mapping)\nClaims the event, downloads the file from Drive, "
            "sends it to the processor, which parses/validates/loads it in ONE transaction and marks it processed only after the commit.\n"
            "Unknown or changed layouts are held (needs_mapping) and a redacted mapping proposal is saved and announced for approval - nothing is guessed. Other files that need a person are announced once by *Discover* (\"Check Attention\").",
         (0, 40), 560, 170)
    return w.export()


# ====================================================================================================== C. Reconcile
def reconcile() -> dict:
    w = WF("MIS - Reconcile Batch")
    w.add("Schedule 10:00 / 14:00 / 18:00 IST", "n8n-nodes-base.scheduleTrigger", 1.2,
          {"rule": {"interval": [{"field": "cronExpression", "expression": "0 10,14,18 * * *"}]}}, (0, 300))
    w.add("Run Now (manual)", "n8n-nodes-base.manualTrigger", 1, {}, (0, 480))
    http(w, "Reconcile (check)", "POST", f"={{{{ {P} }}}}/v1/reconcile/run", (240, 380),
         body="={{ JSON.stringify({ lookback_days: 2 }) }}", retry=True)
    if_(w, "Anything To Retry?", "={{ ($json.retry_event_ids || []).length }}", "number", "gt", 0, (480, 380))
    http(w, "Get Pending Events", "GET", f"={{{{ {P} }}}}/v1/events/pending?limit=200", (720, 260), once=True)
    w.add("Select Retries", "n8n-nodes-base.code", 2, code("""
const wanted = new Set($('Reconcile (check)').first().json.retry_event_ids || []);
const events = $input.first().json.events || [];
return events.filter((e) => wanted.has(e.event_id)).map((e) => ({ json: { event_id: e.event_id, drive_file_id: e.drive_file_id, filename: e.filename } }));
"""), (960, 260))
    http(w, "Retry Process File", "POST", f"={{{{ {HOOK} }}}}/webhook/mis-process-file", (1200, 260), auth="hook",
         body="={{ JSON.stringify({ event_id: $json.event_id, drive_file_id: $json.drive_file_id, filename: $json.filename }) }}",
         never_error=True, timeout=900000)
    http(w, "Reconcile (final)", "POST", f"={{{{ {P} }}}}/v1/reconcile/run", (1440, 380),
         body="={{ JSON.stringify({ lookback_days: 2 }) }}", once=True, retry=True)
    w.add("Batches To Announce", "n8n-nodes-base.code", 2, code("""
// One item per business day whose situation CHANGED since the last announcement (server-side de-duplication).
const out = $input.first().json;
return (out.batches || []).filter((b) => b.notify).map((b) => ({ json: { date: b.date, status: b.status, summary_hash: b.summary_hash, text: b.message } }));
"""), (1680, 380))
    if_(w, "Notify Configured?", "={{ $env.MIS_NOTIFY_WEBHOOK_URL || '' }}", "string", "notEmpty", None, (1920, 380))
    w.add("Send Summary", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": "={{ $env.MIS_NOTIFY_WEBHOOK_URL }}", "sendBody": True, "contentType": "json",
        "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ text: $json.text }) }}", "options": {"timeout": 20000}},
        (2160, 300), onError="continueErrorOutput")
    http(w, "Acknowledge", "POST", f"={{{{ {P} }}}}/v1/reconcile/ack", (2400, 300),
         body="={{ JSON.stringify({ date: $('Batches To Announce').all()[$itemIndex].json.date, summary_hash: $('Batches To Announce').all()[$itemIndex].json.summary_hash }) }}")
    w.chain("Schedule 10:00 / 14:00 / 18:00 IST", "Reconcile (check)")
    w.chain("Run Now (manual)", "Reconcile (check)")
    w.chain("Reconcile (check)", "Anything To Retry?")
    w.link("Anything To Retry?", "Get Pending Events", 0)
    w.link("Anything To Retry?", "Reconcile (final)", 1)
    w.chain("Get Pending Events", "Select Retries", "Retry Process File", "Reconcile (final)")
    w.chain("Reconcile (final)", "Batches To Announce", "Notify Configured?")
    w.link("Notify Configured?", "Send Summary", 0)
    w.link("Send Summary", "Acknowledge", 0)
    note(w, "**Reconcile**\nCompares the expected-report manifest (config/expected_reports.yaml) with the ledger for today and the previous day, "
            "re-runs transient failures, then announces only what changed. A day is COMPLETE when every required report is processed.\n"
            "Notifications carry report ids, counts and file names only.", (0, 40), 520, 170)
    return w.export()


# ====================================================================================================== D. Approve mapping
def approve() -> dict:
    w = WF("MIS - Approve Mapping")
    w.add("Webhook", "n8n-nodes-base.webhook", 2, {"httpMethod": "POST", "path": "mis-approve-mapping",
          "authentication": "headerAuth", "responseMode": "lastNode", "options": {}},
          (0, 300), creds=CRED_HOOK, webhookId=str(uuid.uuid5(NS, "webhook/approve")))
    w.add("Validate Input", "n8n-nodes-base.code", 2, code("""
const b = $input.first().json.body || {};
const id = Number(b.profile_id);
if (!Number.isInteger(id) || id <= 0) throw new Error('profile_id must be a positive integer');
if (!b.approved_by || !String(b.approved_by).trim()) throw new Error('approved_by (who is approving) is required');
return [{ json: { profile_id: id, approved_by: String(b.approved_by).trim(), accept_flags: Array.isArray(b.accept_flags) ? b.accept_flags.map(String) : [] } }];
"""), (220, 300))
    http(w, "Approve And Activate", "POST", f"={{{{ {P} + '/v1/mapping/' + $json.profile_id + '/approve' }}}}", (440, 300),
         body="={{ JSON.stringify({ approved_by: $json.approved_by, accept_flags: $json.accept_flags }) }}", never_error=True)
    if_(w, "Approved?", "={{ $json.status || '' }}", "string", "equals", "active", (660, 300))
    http(w, "Get Pending Events", "GET", f"={{{{ {P} }}}}/v1/events/pending?limit=200", (880, 220), once=True)
    w.add("Split Events", "n8n-nodes-base.code", 2, code("""
const events = $input.first().json.events || [];
return events.map((e) => ({ json: { event_id: e.event_id, drive_file_id: e.drive_file_id, filename: e.filename } }));
"""), (1100, 220))
    http(w, "Process File", "POST", f"={{{{ {HOOK} }}}}/webhook/mis-process-file", (1320, 220), auth="hook",
         body="={{ JSON.stringify({ event_id: $json.event_id, drive_file_id: $json.drive_file_id, filename: $json.filename }) }}",
         never_error=True, timeout=900000)
    w.add("Result", "n8n-nodes-base.code", 2, code("""
const rows = $input.all().map((i) => i.json);
const approval = $('Approve And Activate').first().json;
return [{ json: { approved: true, profile_id: approval.profile_id, requeued_events: approval.requeued_events || [], processed: rows.map((r) => ({ event_id: r.event_id, outcome: r.outcome })) } }];
"""), (1540, 220))
    w.add("Rejected", "n8n-nodes-base.code", 2, code("""
const r = $input.first().json;
return [{ json: { approved: false, error: r.error || 'not approved', message: r.message || '' } }];
"""), (880, 420))
    w.chain("Webhook", "Validate Input", "Approve And Activate", "Approved?")
    w.link("Approved?", "Get Pending Events", 0)
    w.link("Approved?", "Rejected", 1)
    w.chain("Get Pending Events", "Split Events", "Process File", "Result")
    note(w, "**Approve a mapping proposal**\nPOST {approved_by, profile_id, accept_flags:[...]}. The processor refuses approval while blocking findings or open "
            "questions exist, or when a review flag (e.g. net_disbursed_amount_semantics) is not accepted by name. On success the held files are processed.",
         (0, 40), 560, 160)
    return w.export()


def main():
    OUT.mkdir(exist_ok=True)
    files = {"mis_discover_files.json": discover(), "mis_process_file.json": process(),
             "mis_reconcile_batch.json": reconcile(), "mis_approve_mapping.json": approve()}
    for fn, doc in files.items():
        (OUT / fn).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote", OUT / fn, len(doc["nodes"]), "nodes")


if __name__ == "__main__":
    main()
