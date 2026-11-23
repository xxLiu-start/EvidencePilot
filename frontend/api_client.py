"""Small HTTP boundary; no database access, dotenv loading or model credentials."""
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class APIError(RuntimeError):
    def __init__(self, category, status_code=None):
        self.category, self.status_code = category, status_code
        super().__init__(category)


def safe_http_url(value):
    try:
        url = urlsplit(value or "")
        return bool(url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password)
    except ValueError:
        return False


def identifier(value):
    return str(UUID(str(value)))


@dataclass
class APIClient:
    base_url: str
    transport: object = None
    access_token: str | None = None
    project_id: str | None = None

    def __post_init__(self):
        if not safe_http_url(self.base_url) or urlsplit(self.base_url).query or urlsplit(self.base_url).fragment:
            raise ValueError("API_BASE_URL must be an HTTP(S) service address without credentials")
        self.base_url = self.base_url.rstrip("/")

    def request(self, method, path, **kwargs):
        # A lost POST response is ambiguous: never silently repeat task/upload writes.
        timeout = kwargs.pop("timeout", 10)
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.project_id:
            headers["X-Project-ID"] = self.project_id
        if headers:
            kwargs["headers"] = headers
        try:
            with httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False,
                              follow_redirects=False, transport=self.transport) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise APIError("timeout") from None
        except httpx.HTTPError:
            raise APIError("unreachable") from None
        if response.is_error or response.is_redirect:
            # Do not show arbitrary upstream response bodies (may contain secrets).
            category = {404: "not_found", 409: "conflict", 413: "too_large", 415: "file_type",
                        422: "validation", 503: "unavailable"}.get(response.status_code, "http_error")
            raise APIError(category, response.status_code)
        try:
            return response.json()
        except ValueError:
            raise APIError("invalid_response") from None

    def health(self): return self.request("GET", "/health")
    def system(self): return self.request("GET", "/api/v1/system")
    def create(self, query, budget): return self.request("POST", "/api/v1/research", json={"query": query, "budget": budget})
    def tasks(self, offset=0): return self.request("GET", "/api/v1/tasks", params={"offset": offset, "limit": 20})
    def task(self, task_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}")
    def trace(self, task_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/trace")
    def report(self, task_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/report")
    def agent(self, task_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/agent")
    def evidences(self, task_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/evidences")
    def evidence(self, task_id, evidence_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/evidences/{identifier(evidence_id)}")
    def verifications(self, task_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/verifications")
    def recover(self, task_id): return self.request("POST", f"/api/v1/tasks/{identifier(task_id)}/recover")
    def reviews(self, task_id, claim_id): return self.request("GET", f"/api/v1/tasks/{identifier(task_id)}/claims/{claim_id}/reviews")
    def submit_review(self, task_id, claim_id, fingerprint, decision, comment):
        return self.request("POST", f"/api/v1/tasks/{identifier(task_id)}/claims/{claim_id}/reviews",
                            json={"fingerprint": fingerprint, "decision": decision, "comment": comment})
    def feedback(self, task_id, rating, comment): return self.request("POST", f"/api/v1/tasks/{identifier(task_id)}/feedback", json={"rating": rating, "comment": comment})
    def documents(self, offset=0): return self.request("GET", "/api/v1/documents", params={"offset": offset, "limit": 20})
    def chunks(self, doc_id): return self.request("GET", f"/api/v1/documents/{identifier(doc_id)}/chunks")
    def evaluations(self, offset=0): return self.request("GET", "/api/v1/evaluations", params={"offset": offset, "limit": 100})
    def upload(self, name, content, **fields):
        return self.request("POST", "/api/v1/documents/upload", files={"file": (name, content)}, data=fields, timeout=300)
