"""Disposable cross-container TLS, real native inference and recovery test.

Uses tiny randomly initialized local models, not BGE quality benchmarks.
Requires the two CI-built images, Docker and OpenSSL. No external services.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from uuid import uuid4

FIXTURE = r"""
import hashlib, json
from pathlib import Path
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing
from transformers import BertConfig, BertModel, BertForSequenceClassification, PreTrainedTokenizerFast

vocab = {"[PAD]":0,"[UNK]":1,"[CLS]":2,"[SEP]":3,"hello":4,"world":5}
tok = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
tok.pre_tokenizer = Whitespace()
tok.post_processor = TemplateProcessing(single="[CLS] $A [SEP]", pair="[CLS] $A [SEP] $B:1 [SEP]:1",
    special_tokens=[("[CLS]",2),("[SEP]",3)])
tokenizer = PreTrainedTokenizerFast(tokenizer_object=tok, pad_token="[PAD]", unk_token="[UNK]",
    cls_token="[CLS]", sep_token="[SEP]", model_input_names=["input_ids","attention_mask","token_type_ids"])
for kind, revision, size, heads, klass in [
    ("embedding", "1"*40, 1024, 8, BertModel),
    ("reranker", "2"*40, 32, 4, BertForSequenceClassification),
]:
    root = Path("/fixture") / kind
    root.mkdir()
    config = BertConfig(vocab_size=len(vocab), hidden_size=size, num_attention_heads=heads,
        num_hidden_layers=1, intermediate_size=64, max_position_embeddings=128, num_labels=1)
    klass(config).save_pretrained(root, safe_serialization=True)
    tokenizer.save_pretrained(root)
    for item in root.iterdir():
        item.chmod(0o644)
    root.chmod(0o755)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    (root/"manifest.json").write_text(json.dumps({"revision":revision,"files":files}))
    (root/"manifest.json").chmod(0o644)
"""

CLIENT = r"""
import concurrent.futures, math, ssl, time
import httpx
from app.config import settings
from app.retrieval.remote import request_inference

base = settings.inference_url
token = settings.inference_token.get_secret_value()
headers = {"Authorization":"Bearer "+token}
tls = ssl.create_default_context(cafile=settings.inference_ca_file)
with httpx.Client(verify=tls, timeout=30, trust_env=False) as client:
    last_error = "no response"
    for attempt in range(90):
        try:
            if client.get(base+"/health").status_code == 200: break
        except httpx.HTTPError as exc: last_error = str(exc)
        time.sleep(1)
    else: raise RuntimeError("TLS health check timed out: " + last_error)
    assert client.post(base+"/v1/embeddings",json={"texts":["hello"]}).status_code == 401
    assert client.post(base+"/v1/embeddings",json={"texts":["hello"]},headers={"Authorization":"Bearer wrong"}).status_code == 401
    assert client.get(base+"/metrics",headers=headers).status_code == 401
    assert client.post(base+"/v1/embeddings",json={"texts":[]},headers=headers).status_code == 422
    try:
        with httpx.Client(trust_env=False,timeout=5) as untrusted:
            untrusted.get(base+"/health")
    except httpx.ConnectError: pass
    else: raise AssertionError("Untrusted CA was accepted")
    try:
        client.get(base.replace("://inference:","://wrong-host:")+"/health")
    except httpx.ConnectError: pass
    else: raise AssertionError("Wrong TLS hostname was accepted")
    vectors = request_inference(settings,"/v1/embeddings",{"texts":["hello","world"]},"embedding")
    assert len(vectors)==2 and all(len(v)==1024 and abs(sum(x*x for x in v)-1)<1e-4 for v in vectors)
    scores = request_inference(settings,"/v1/rerank",{"query":"hello","texts":["world","hello"]},"reranker")
    assert len(scores)==2 and all(math.isfinite(x) and 0<=x<=1 for x in scores)
    bad = settings.model_copy(update={"embedding_revision":"3"*40})
    try: request_inference(bad,"/v1/embeddings",{"texts":["hello"]},"embedding")
    except ValueError: pass
    else: raise AssertionError("Incorrect model revision was accepted")
    def request(_):
        started = time.monotonic()
        response = client.post(base+"/v1/embeddings",json={"texts":["hello"]*16},headers=headers)
        return response.status_code, time.monotonic()-started
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(request,range(24)))
    assert all(code in (200,429,503) for code,_ in results)
    assert any(code==200 for code,_ in results)
    print("Concurrent fixture: successes",sum(code==200 for code,_ in results),
          "capacity rejections",sum(code in (429,503) for code,_ in results),
          "max seconds",round(max(t for _,t in results),3))
    assert client.post(base+"/v1/embeddings",json={"texts":["hello"]},headers=headers).status_code==200
print("PASS: actual TLS, CA and hostname rejection, authorization, model identity, native inference and bounded concurrent requests")
"""

def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def main():
    name = "ep-inference-" + uuid4().hex[:12]
    network = name + "-net"
    with tempfile.TemporaryDirectory(prefix="ep-inference-") as tmp:
        root = Path(tmp)
        root.chmod(0o755)  # only the disposable fixture is mounted into the test containers
        try:
            run("docker","network","create","--internal",network,stdout=subprocess.DEVNULL)
            run("docker","run","--rm","--network","none","--user",f"{os.getuid()}:{os.getgid()}","-v",tmp+":/fixture",
                "evidencepilot-inference:ci","-c",FIXTURE)
            run("openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                "-subj","/CN=ep-test-ca","-keyout",str(root/"ca.key"),"-out",str(root/"ca.crt"),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            run("openssl","req","-newkey","rsa:2048","-nodes","-subj","/CN=inference",
                "-keyout",str(root/"tls.key"),"-out",str(root/"tls.csr"),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            (root/"ext").write_text("subjectAltName=DNS:inference\nextendedKeyUsage=serverAuth\n")
            run("openssl","x509","-req","-in",str(root/"tls.csr"),"-CA",str(root/"ca.crt"),
                "-CAkey",str(root/"ca.key"),"-CAcreateserial","-days","1","-extfile",str(root/"ext"),
                "-out",str(root/"tls.crt"),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            (root/"ca.key").unlink()
            for filename in ("tls.key","tls.crt","ca.crt"):
                (root/filename).chmod(0o644)
            token=secrets.token_urlsafe(48)
            metrics=secrets.token_urlsafe(48)
            for filename,value in (("token",token),("metrics",metrics)):
                (root/filename).write_text(value)
                (root/filename).chmod(0o644)
            env=["-e","DATABASE_URL=sqlite://","-e","ENVIRONMENT=production"]
            for kind,revision in (("embedding","1"*40),("reranker","2"*40)):
                env += ["-e",kind.upper()+"_MODEL=/models/"+kind,
                        "-e",kind.upper()+"_REVISION="+revision,
                        "-e",kind.upper()+"_MAX_LENGTH=64"]
            server=env[:] + ["-e","HF_HOME=/tmp/huggingface","-e","HOME=/tmp"]
            # Exercise the exact production manifest policy before starting HTTPS.
            for kind in ("embedding","reranker"):
                digest=hashlib.sha256((root/kind/"manifest.json").read_bytes()).hexdigest()
                server += ["-e",kind.upper()+"_MANIFEST_SHA256="+digest,
                           "-e",kind.upper()+"_LOCAL_FILES_ONLY=true",
                           "-v",str(root/kind)+":/models/"+kind+":ro"]
            run("docker","run","--rm","--network","none",*server,
                "evidencepilot-inference:ci","python","-c",
                "from app.config import settings; from app.retrieval.model_integrity import verified_model_path; [verified_model_path(settings,k) for k in (\"embedding\",\"reranker\")]; print(\"PASS: native fixture manifests\")")
            run("docker","run","-d","--name",name,"--network",network,
                "--network-alias","inference","--network-alias","wrong-host",
                "--read-only","--tmpfs","/tmp:mode=1777","--cap-drop","ALL",
                "--security-opt","no-new-privileges:true",*server,
                "-v",str(root/"token")+":/run/secrets/inference_token:ro",
                "-v",str(root/"metrics")+":/run/secrets/inference_metrics_token:ro",
                "-v",str(root/"tls.key")+":/run/secrets/tls.key:ro",
                "-v",str(root/"tls.crt")+":/run/secrets/tls.crt:ro",
                "evidencepilot-inference:ci","-m","uvicorn","app.inference_service:app",
                "--host","0.0.0.0","--port","8000","--ssl-keyfile","/run/secrets/tls.key",
                "--ssl-certfile","/run/secrets/tls.crt","--no-access-log",
                stdout=subprocess.DEVNULL)
            client=["docker","run","--rm","--network",network,*env,
                "-e","INFERENCE_MODE=remote","-e","INFERENCE_URL=https://inference:8000",
                "-e","INFERENCE_CA_FILE=/run/secrets/ca.crt",
                "-v",str(root/"ca.crt")+":/run/secrets/ca.crt:ro",
                "-v",str(root/"token")+":/run/secrets/inference_token:ro",
                "evidencepilot-business:ci","-c"]
            run(*client,CLIENT)
            run("docker","restart",name,stdout=subprocess.DEVNULL)
            run(*client,CLIENT)
            print("PASS: inference container restarted and repeat acceptance passed")
        except Exception:
            # Synthetic fixture only; report server startup trace before removal.
            subprocess.run(["docker","logs","--tail","80",name],check=False)
            raise
        finally:
            subprocess.run(["docker","rm","-f",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(["docker","network","rm",network],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=="__main__":
    main()
