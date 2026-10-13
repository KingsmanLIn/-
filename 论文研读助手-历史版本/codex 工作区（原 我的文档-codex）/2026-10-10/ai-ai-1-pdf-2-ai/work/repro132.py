"""Independent review probes. No real keys, real papers or external endpoints."""
from pathlib import Path
import sys, os, tempfile, uuid, threading, json, time
from unittest import mock
ROOT=Path(__file__).resolve().parent
CODE=ROOT/"review-1.3.2"
sys.path[:0]=[str(CODE),str(CODE/"tests"),str(CODE/"tests/security")]
TMP=ROOT/"repro132-data"
TMP.mkdir(exist_ok=True)
def mkdtemp(suffix=None,prefix=None,dir=None):
    p=Path(dir or TMP)/((prefix or "tmp")+uuid.uuid4().hex+(suffix or ""))
    p.mkdir(mode=0o777)
    return str(p)
tempfile.mkdtemp=mkdtemp
tempfile.tempdir=str(TMP)
os.environ["PAPER_READER_NO_DIALOG"]="1"
import server, analysis, providers, reading_card, secret_guard, job_policy, quotes
import endpoint_policy, secure_transport
from baseline_helpers import fresh_library_v1
from card_fixtures import build_card,pages_from_prompt
from stubs import CountingSink
PAPER="medical-2026-zang"
results={}
def new_store(label):
    d=Path(mkdtemp(prefix=label+"-"))
    fresh_library_v1(d)
    return server.Store(d)
def fake():
    f=providers.FakeProvider(script=["success"],payload={})
    f.payload_for=lambda req,payload:build_card(pages_from_prompt(req.user))
    return f

# 1. Actual loopback POST reaches NEW destination though snapshot/approval name OLD.
with CountingSink(reply="ok",style="openai") as old_sink, CountingSink(reply="ok",style="openai") as new_sink:
    s=new_store("target")
    initial={"provider":"openai_compatible","mode":"local_loopback",
             "base_url":f"http://127.0.0.1:{old_sink.port}/v1","model":"test-model",
             "api_key_env":"PAPER_READER_REVIEW_OLD"}
    s.update_ai_settings(initial)
    m=analysis.AnalysisManager(s)
    m.enqueue(PAPER,reason="manual")
    old_job=m.ledger.frozen_job(PAPER)
    s.update_ai_settings({"base_url":f"http://127.0.0.1:{new_sink.port}/v1",
                          "api_key_env":"PAPER_READER_REVIEW_NEW"})
    old_approval=m.ledger.approval_for(PAPER)
    with mock.patch.dict(os.environ,{"PAPER_READER_REVIEW_OLD":"review-only-old-credential-132",
                                    "PAPER_READER_REVIEW_NEW":"review-only-new-credential-132"}):
        m.process_once()
    results["target_mismatch"]={
        "old_target_received":old_sink.count,"new_target_received":new_sink.count,
        "new_target_received_auth":new_sink.any_auth(),
        "frozen_target_is_old":old_job.target_identity==old_approval["target_identity"],
        "target_changed":s.endpoint_identity()!=old_job.target_identity,
        "final_status":s.analysis_meta(PAPER)["status"]}

# 2. A late failure of generation A overwrites generation B's ready state.
class LateFailure(providers.ProviderAdapter):
    name="review-failure"
    def __init__(self):self.enter=threading.Event(); self.release=threading.Event(); self.request_sent=True
    def complete_json(self,request,on_start=None):
        self.enter.set(); self.release.wait(10)
        return providers.AdapterResult(False,category=providers.CATEGORY_AUTH,
                  public_code=secret_guard.E_AUTH_FAILED,message="fixed review failure")
    def cancel(self):return True
s=new_store("late-failure")
s.update_ai_settings({"provider":"ollama","model":"stub","max_concurrent_jobs":2})
first=LateFailure(); second=fake()
m=analysis.AnalysisManager(s,provider_factory=lambda settings:first)
m.enqueue(PAPER,reason="manual")
t=threading.Thread(target=m.process,args=(PAPER,));t.start();assert first.enter.wait(10)
old=m.ledger.frozen_job(PAPER)
m.provider_factory=lambda settings:second
m.enqueue(PAPER,reason="force",force=True);m.process(PAPER)
before=s.analysis_meta(PAPER)["status"]
first.release.set();t.join(15)
results["late_failure"]={"status_after_new_success":before,"status_after_old_failure":s.analysis_meta(PAPER)["status"],
                         "answer_file_still_exists":s.files.has_analysis(PAPER),
                         "current_job_is_new":m.ledger.frozen_job(PAPER).job_id!=old.job_id}

# 3. The production adapter parser accepts a duplicate-key card; strict helper rejects.
pages=[{"pdf_page":i,"text":f"Review synthetic page {i}. Evidence and sample details."*4} for i in range(1,5)]
card=build_card(pages)
raw=json.dumps(card,ensure_ascii=False)
duplicate=raw[:-1]+',"overall":'+json.dumps(card["overall"],ensure_ascii=False)+"}"
req=providers.CompletionRequest(system="s",user="u",model="m")
parsed,err=providers.HttpAdapter._load_json(duplicate,req)
validated=reading_card.validate_reading_card(parsed,quotes.PageTextIndex(pages),4,paper_id=PAPER)
try:reading_card.parse_model_json(duplicate); strict_rejects=False
except reading_card.ValidationError:strict_rejects=True
results["duplicate_json"]={"production_parser_accepts":err is None,
                           "production_validator_accepts":bool(validated),
                           "strict_helper_rejects":strict_rejects}

# 4. Spend truncation allows more than configured daily ceiling.
ledger=job_policy.JobLedger(TMP/"budget")
limits={"daily_request_limit":1001,"max_requests_per_job":4}
accepted=0
for i in range(1003):
    reservation,code=ledger.reserve(None,limits,paper_id=str(i))
    if reservation is not None:accepted+=1
results["budget_rollover"]={"daily_limit":1001,"accepted_reservations":accepted,
                            "reported_spend":ledger.spend_today(),"journal_length":len(ledger.state["journal"])}

# 5. Key-like values accepted by credentials but never registered by the guard.
short="abcde"
valid=__import__("credentials").validate_value(short)[0]
secret_guard.register(short)
results["short_secret"]={"credential_accepted":valid,"echo_detected":secret_guard.contains_secret(short)}

# 6. A normal /models response echoes a full known Key to browser HTTP response.
from tls_stub import TlsStub
import http.client
sentinel="review-health-credential-132-only"
with TlsStub(tls=False,payload={"data":[{"id":sentinel}]}) as stub:
    s=new_store("health")
    s.update_ai_settings({"provider":"openai_compatible","mode":"local_loopback",
                         "base_url":f"http://127.0.0.1:{stub.port}/v1","model":"",
                         "api_key_env":"PAPER_READER_REVIEW_HEALTH"})
    m=analysis.AnalysisManager(s);m.approve_target()
    httpd=server.ThreadingHTTPServer(("127.0.0.1",0),server.handler_for(s,"review-token",m))
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:
        with mock.patch.dict(os.environ,{"PAPER_READER_REVIEW_HEALTH":sentinel}):
            c=http.client.HTTPConnection("127.0.0.1",httpd.server_port,timeout=5)
            c.request("GET","/api/analysis",headers={"X-Reader-Token":"review-token"})
            response=c.getresponse(); body=response.read();c.close()
        results["health_echo"]={"http_status":response.status,
                                "browser_response_contains_known_key":sentinel.encode() in body,
                                "guard_knows_key":secret_guard.contains_secret(sentinel),
                                "upstream_get_count":stub.count}
    finally:httpd.shutdown();httpd.server_close()

# 7. User cancellation during first POST does not stop an adaptation retransmission.
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
enter=threading.Event();release=threading.Event();counter=[]
class AdaptHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        counter.append(1)
        if len(counter)==1:
            enter.set();release.wait(10);body=b'{"error":"response_format unsupported"}';status=400
        else:body=b'{"choices":[{"message":{"content":"{}"}}]}';status=200
        self.send_response(status);self.send_header("Content-Length",str(len(body)));self.end_headers()
        self.wfile.write(body)
stub=ThreadingHTTPServer(("127.0.0.1",0),AdaptHandler)
threading.Thread(target=stub.serve_forever,daemon=True).start()
try:
    s=new_store("cancel")
    s.update_ai_settings({"provider":"openai_compatible","mode":"local_loopback",
                          "base_url":f"http://127.0.0.1:{stub.server_port}/v1","model":"stub"})
    m=analysis.AnalysisManager(s);m.enqueue(PAPER,reason="manual")
    t=threading.Thread(target=m.process_once);t.start();assert enter.wait(10)
    cancelled=m.cancel(PAPER);release.set();t.join(15)
    results["cancel_adaptation"]={"cancel_acknowledged":cancelled,
                                  "actual_posts":len(counter),
                                  "final_status":s.analysis_meta(PAPER)["status"]}
finally:stub.shutdown();stub.server_close()

# 8. Slow drip exceeds read timeout because there is no whole-response deadline.
class DripHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        body=b"12345678901234567890"
        self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers()
        for byte in body:
            self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.03)
stub=ThreadingHTTPServer(("127.0.0.1",0),DripHandler)
threading.Thread(target=stub.serve_forever,daemon=True).start()
try:
    policy=endpoint_policy.EndpointPolicy.for_settings(f"http://127.0.0.1:{stub.server_port}","local_loopback")
    policy.authorize()
    transport=secure_transport.SecureTransport(policy,read_timeout=.1)
    start=time.monotonic();response=transport.get_json("/models")
    results["drip_deadline"]={"read_timeout":.1,"elapsed":round(time.monotonic()-start,3),
                              "response_ok":response.ok}
finally:stub.shutdown();stub.server_close()

# 9. Shipped UI sends mode to /api/settings, which silently omits it.
s=new_store("ui-mode")
m=analysis.AnalysisManager(s,provider_factory=lambda settings:fake())
httpd=server.ThreadingHTTPServer(("127.0.0.1",0),server.handler_for(s,"review-token",m))
threading.Thread(target=httpd.serve_forever,daemon=True).start()
try:
    payload={"provider":"openai_compatible","mode":"cloud_https","model":"stub",
             "base_url":"https://api.example.com/v1","api_key_env":"",
             "auto_analyze":False,"sync_enabled":False,"vault_path":""}
    c=http.client.HTTPConnection("127.0.0.1",httpd.server_port,timeout=5)
    c.request("POST","/api/settings",body=json.dumps(payload),
              headers={"X-Reader-Token":"review-token","Content-Type":"application/json"})
    response=c.getresponse();parsed=json.loads(response.read());c.close()
    results["ui_mode"]={"requested_mode":"cloud_https","returned_mode":parsed["ai"]["mode"],
                       "stored_mode":s.ai_settings()["mode"],"http_status":response.status}
finally:httpd.shutdown();httpd.server_close()

(ROOT/"repro132-results.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(results,ensure_ascii=True,indent=2))
