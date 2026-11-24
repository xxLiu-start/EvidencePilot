"""Real local Keycloak -> browser -> Streamlit -> FastAPI SSO acceptance.

Run only against disposable test services. All credentials are generated
per invocation. No external identity provider, production data or model is
used. A successful run verifies actual OAuth redirects and JWKS signatures,
not a mocked principal dependency.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ISSUER = "http://localhost:18080/realms/evidencepilot"
API = "http://localhost:18081"
UI = "http://localhost:18501"


def wait_http(url, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                if client.get(url).status_code == 200:
                    return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError("Disposable test service did not become ready: " + url)


def login_token(username, password):
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.post(ISSUER + "/protocol/openid-connect/token", data={
            "client_id": "evidencepilot-cli", "grant_type": "password",
            "username": username, "password": password, "scope": "openid",
        })
        response.raise_for_status()
        return response.json()


def provision(tokens):
    from sqlalchemy.orm import Session
    from app.auth.oidc import verify_access_token
    from app.db.models import Tenant, Project, User, TenantMembership, ProjectMembership, ResearchTask
    from app.db.session import Base, engine

    Base.metadata.create_all(engine)
    ids = {}
    with Session(engine) as db:
        for name in ("alice", "bob"):
            token = tokens[name]["access_token"]
            try:
                principal = verify_access_token(token)
            except Exception:
                # Only report comparisons and error categories, never JWTs,
                # subject IDs, emails, keys or credentials.
                import jwt
                claims = jwt.decode(token, options={"verify_signature": False})
                audience = claims.get("aud", [])
                audience = [audience] if isinstance(audience, str) else audience
                print("OIDC diagnostic:",
                      "issuer_matches=" + str(claims.get("iss") == ISSUER),
                      "api_audience_present=" + str("evidencepilot-api" in audience),
                      "algorithm=" + str(jwt.get_unverified_header(token).get("alg")),
                      "has_required_claims=" + str(all(key in claims for key in ("exp", "iat", "iss", "sub", "aud"))))
                raise
            tenant = Tenant(slug="sso-" + name, name="SSO " + name)
            user = User(issuer=principal.issuer, subject=principal.subject)
            db.add_all([tenant, user])
            db.flush()
            project = Project(tenant_id=tenant.id, slug="default", name="Default")
            db.add(project)
            db.flush()
            db.add_all([
                TenantMembership(tenant_id=tenant.id, user_id=user.id, role="VIEWER"),
                ProjectMembership(project_id=project.id, user_id=user.id, role="VIEWER"),
            ])
            task = ResearchTask(query="SSO " + name + " private task", budget=1, status="partial",
                                tenant_id=tenant.id, project_id=project.id,
                                created_by_user_id=user.id)
            db.add(task)
            db.flush()
            ids[name] = str(task.id)
        db.commit()
    return ids


def assert_api(tokens, ids):
    with httpx.Client(base_url=API, timeout=10, trust_env=False) as client:
        assert client.get("/api/v1/auth/me").status_code == 401
        for name, other in (("alice", "bob"), ("bob", "alice")):
            headers = {"Authorization": "Bearer " + tokens[name]["access_token"]}
            identity = client.get("/api/v1/auth/me", headers=headers)
            assert identity.status_code == 200, identity.status_code
            assert identity.json()["tenant_role"] == "VIEWER"
            history = client.get("/api/v1/tasks", headers=headers)
            assert history.status_code == 200
            assert {item["id"] for item in history.json()["items"]} == {ids[name]}
            assert client.get("/api/v1/tasks/" + ids[other], headers=headers).status_code == 404
            assert client.post("/api/v1/research", headers=headers,
                               json={"query": "not authorized", "budget": 1}).status_code == 403
            assert client.get("/api/v1/auth/me", headers={
                "Authorization": "Bearer " + tokens[name]["id_token"]
            }).status_code == 401
        assert client.get("/api/v1/auth/me", headers={
            "Authorization": "Bearer " + tokens["unprovisioned"]["access_token"]
        }).status_code == 403


def assert_browser(passwords):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for name, other in (("alice", "bob"), ("bob", "alice")):
                context = browser.new_context()
                try:
                    page = context.new_page()
                    page.goto(UI, wait_until="domcontentloaded")
                    page.get_by_role("button", name="使用企业账号登录").click(timeout=60000)
                    page.locator("#username").fill(name, timeout=60000)
                    page.locator("#password").fill(passwords[name])
                    page.locator("#kc-login").click()
                    expect(page.get_by_text("SSO " + name + " private task", exact=True)).to_be_visible(timeout=90000)
                    expect(page.get_by_text("SSO " + other + " private task", exact=True)).to_have_count(0)
                    page.get_by_role("button", name="退出企业登录").click()
                    # Keycloak may request logout confirmation. Never reuse the
                    # identity cookie or token after an explicit logout.
                    try:
                        page.locator("#kc-logout").click(timeout=5000)
                    except Exception:
                        pass
                    page.goto(UI, wait_until="domcontentloaded")
                    expect(page.get_by_role("button", name="使用企业账号登录")).to_be_visible(timeout=60000)
                finally:
                    context.close()
        finally:
            browser.close()


def main():
    secret_path = ROOT / ".streamlit" / "secrets.toml"
    if secret_path.exists():
        raise RuntimeError("Refusing to replace existing Streamlit secrets")
    passwords = {name: secrets.token_urlsafe(24) for name in ("alice", "bob", "unprovisioned")}
    client_secret = secrets.token_urlsafe(32)
    cookie_secret = secrets.token_urlsafe(48)
    container_name = "ep-sso-" + uuid4().hex[:12]
    processes = []
    try:
        with tempfile.TemporaryDirectory(prefix="ep-sso-") as tmp:
            root = Path(tmp)
            realm = json.loads((ROOT / "deploy/keycloak/evidencepilot-realm.json").read_text())
            realm["users"] = [{
                "username": name, "enabled": True, "email": name + "@example.test",
                "emailVerified": True, "firstName": name, "lastName": "test",
                "credentials": [{"type": "password", "value": password, "temporary": False}],
                # A forged high-privilege realm role must not override DB membership.
                "realmRoles": ["SUPER_ADMIN"],
            } for name, password in passwords.items()]
            # Use explicit per-client audience mappers as well as the named
            # scope; realm imports may resolve default client scopes differently.
            mapper = realm["clientScopes"][0]["protocolMappers"][0]
            cli = next(item for item in realm["clients"] if item["clientId"] == "evidencepilot-cli")
            cli["protocolMappers"] = [mapper]
            realm["clients"].append({
                "clientId": "evidencepilot-streamlit", "enabled": True,
                "protocol": "openid-connect", "publicClient": False, "secret": client_secret,
                "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
                "protocolMappers": [mapper],
                "redirectUris": [UI + "/oauth2callback"], "webOrigins": [UI],
                "attributes": {"post.logout.redirect.uris": UI + "/*"},
                "defaultClientScopes": ["profile", "email", "roles", "evidencepilot-api-audience"],
            })
            (root / "realm.json").write_text(json.dumps(realm))
            # Keycloak runs as an unprivileged container UID. These disposable
            # synthetic credentials are readable only for the duration of the CI run.
            root.chmod(0o755)
            (root / "realm.json").chmod(0o644)
            subprocess.run([
                "docker", "run", "--rm", "-d", "--name", container_name,
                "-p", "127.0.0.1:18080:8080", "-e", "KC_HOSTNAME=http://localhost:18080",
                "-v", str(root / "realm.json") + ":/opt/keycloak/data/import/realm.json:ro",
                "quay.io/keycloak/keycloak:26.1.4", "start-dev", "--import-realm",
            ], check=True, stdout=subprocess.DEVNULL)
            wait_http(ISSUER + "/.well-known/openid-configuration")

            os.environ.update({
                "DATABASE_URL": "sqlite:///" + str(root / "sso.sqlite"),
                "ENVIRONMENT": "test", "AUTH_ENABLED": "true", "DB_AUTO_MIGRATE": "true",
                "TASK_QUEUE_ENABLED": "false", "OIDC_ISSUER": ISSUER,
                "OIDC_AUDIENCE": "evidencepilot-api",
                "OIDC_JWKS_URL": ISSUER + "/protocol/openid-connect/certs",
                "LLM_MODE": "local", "LLM_LOCAL_BASE_URL": "http://127.0.0.1:9/v1",
                "LLM_LOCAL_MODEL": "not-used-in-sso-test",
                "STREAMLIT_OIDC_ENABLED": "true", "API_BASE_URL": API,
            })
            tokens = {name: login_token(name, pwd) for name, pwd in passwords.items()}
            ids = provision(tokens)
            secret_path.parent.mkdir(exist_ok=True)
            secret_path.write_text(
                '[auth]\nredirect_uri = "' + UI + '/oauth2callback"\n'
                + 'cookie_secret = "' + cookie_secret + '"\n'
                + 'client_id = "evidencepilot-streamlit"\nclient_secret = "' + client_secret + '"\n'
                + 'server_metadata_url = "' + ISSUER + '/.well-known/openid-configuration"\n'
                + 'expose_tokens = ["access"]\n'
            )
            secret_path.chmod(0o600)
            with (root / "api.log").open("w") as api_log, (root / "ui.log").open("w") as ui_log:
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18081"],
                    cwd=ROOT, stdout=api_log, stderr=subprocess.STDOUT))
                wait_http(API + "/health")
                assert_api(tokens, ids)
                print("PASS: real Keycloak JWKS, issuer/audience, DB role and tenant authorization")
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", "streamlit", "run", "frontend/streamlit_app.py",
                     "--server.address=127.0.0.1", "--server.port=18501", "--server.headless=true"],
                    cwd=ROOT, stdout=ui_log, stderr=subprocess.STDOUT))
                wait_http(UI + "/_stcore/health")
                assert_browser(passwords)
                print("PASS: actual Chromium OIDC redirect/callback, two-user UI isolation and logout")
    finally:
        for process in processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        secret_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
