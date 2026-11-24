#!/usr/bin/env bash
# Exercise actual Docker networking and TLS; deterministic model doubles isolate transport.
set -euo pipefail
root=$(mktemp -d)
suffix=$(python -c 'import secrets; print(secrets.token_hex(6))')
network="ep-test-$suffix"
server="ep-inference-$suffix"
cleanup() {
  docker rm -f "$server" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT
chmod 755 "$root"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=ep-test-ca -keyout "$root/ca.key" -out "$root/ca.crt" 2>/dev/null
openssl req -new -newkey rsa:2048 -nodes -subj /CN=inference -keyout "$root/server.key" -out "$root/server.csr" 2>/dev/null
printf 'subjectAltName=DNS:inference\nextendedKeyUsage=serverAuth\n' > "$root/ext"
openssl x509 -req -in "$root/server.csr" -CA "$root/ca.crt" -CAkey "$root/ca.key" -CAcreateserial -days 1 -extfile "$root/ext" -out "$root/server.crt" 2>/dev/null
# Only throwaway CI credentials; readable by the non-root container user.
chmod 644 "$root/server.key" "$root/server.crt" "$root/ca.crt"
token=$(python -c 'import secrets; print(secrets.token_hex(32))')
metric=$(python -c 'import secrets; print(secrets.token_hex(32))')
cat > "$root/fixture.py" <<'PY'
from time import sleep
from app import inference_service as svc

def embed(texts):
    sleep(.2)
    return [[1.] + [0.] * 1023 for _ in texts]
svc.embedding.embed_documents = embed
svc.reranker.score = lambda query, candidates: [.8] * len(candidates)
app = svc.app
PY
chmod 644 "$root/fixture.py"
docker network create --internal "$network" >/dev/null
docker run -d --name "$server" --network "$network" --network-alias inference \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -e ENVIRONMENT=test -e "INFERENCE_TOKEN=$token" -e "INFERENCE_METRICS_TOKEN=$metric" \
  -v "$root/fixture.py:/workspace/fixture.py:ro" \
  -v "$root/server.crt:/tls/cert:ro" -v "$root/server.key:/tls/key:ro" \
  evidencepilot-inference:ci python -m uvicorn fixture:app --host 0.0.0.0 --port 8000 \
  --ssl-certfile /tls/cert --ssl-keyfile /tls/key --no-access-log >/dev/null
client() {
  docker run --rm -i --network "$network" -e DATABASE_URL=sqlite:// \
    -e ENVIRONMENT=test -e INFERENCE_MODE=remote -e INFERENCE_URL=https://inference:8000 \
    -e INFERENCE_CA_FILE=/tls/ca -e "INFERENCE_TOKEN=$token" -e "INFERENCE_METRICS_TOKEN=$metric" \
    -e INFERENCE_MAX_RETRIES=0 -v "$root/ca.crt:/tls/ca:ro" \
    evidencepilot-business:ci python -
}
client <<'PY'
import ssl, time, httpx
from concurrent.futures import ThreadPoolExecutor
from app.config import settings
from app.retrieval.embedding import BGEEmbeddingService
from app.retrieval.reranker import RerankerService
base = settings.inference_url
ctx = ssl.create_default_context(cafile=settings.inference_ca_file)
token = settings.inference_token.get_secret_value()
metrics = settings.inference_metrics_token.get_secret_value()
auth = {"Authorization": "Bearer " + token}
with httpx.Client(verify=ctx, trust_env=False, timeout=5) as c:
    for _ in range(40):
        try:
            if c.get(base+"/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.5)
    else:
        raise RuntimeError("TLS service unavailable")
    assert c.post(base+"/v1/embeddings", json={"texts":["a"]}).status_code == 401
    assert c.get(base+"/metrics", headers=auth).status_code == 401
    assert c.get(base+"/metrics", headers={"Authorization":"Bearer "+metrics}).status_code == 200
    assert c.post(base+"/v1/embeddings", headers={"Authorization":"Bearer "+metrics}, json={"texts":["a"]}).status_code == 401
    assert BGEEmbeddingService(settings).embed_query("test") == [1.] + [0.] * 1023
    assert RerankerService(settings).score("q",[{"id":"1","text":"a"}]) == [.8]
    def send(_):
        return c.post(base+"/v1/embeddings",headers=auth,json={"texts":["a"]}).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses=list(pool.map(send,range(16)))
    assert 200 in statuses and 429 in statuses and set(statuses) <= {200,429}, statuses
    assert send(0)==200
try:
    httpx.get(base+"/health", trust_env=False, timeout=5)
except httpx.ConnectError:
    pass
else:
    raise AssertionError("Untrusted CA accepted")
print("PASS: actual HTTPS, auth isolation, CA rejection, overload and recovery (model doubles)")
PY
docker stop "$server" >/dev/null
client <<'PY'
from app.config import settings
from app.retrieval.embedding import BGEEmbeddingService, EmbeddingError
try:
    BGEEmbeddingService(settings).embed_query("test")
except EmbeddingError:
    print("PASS: service stopped, no local fallback")
else:
    raise AssertionError("Outage did not fail closed")
PY
docker start "$server" >/dev/null
client <<'PY'
import time
from app.config import settings
from app.retrieval.embedding import BGEEmbeddingService
from app.retrieval.remote import _circuits
from app.retrieval.embedding import EmbeddingError
for _ in range(30):
    try:
        assert len(BGEEmbeddingService(settings).embed_query("test"))==1024
        print("PASS: service restart recovery")
        break
    except EmbeddingError:
        _circuits.clear()
        time.sleep(.5)
else:
    raise RuntimeError("Restart recovery failed")
PY
