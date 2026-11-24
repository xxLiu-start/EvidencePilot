from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr
from typing import Literal

class Settings(BaseSettings):
    database_url: str
    environment: Literal["development", "test", "production"] = "development"
    auth_enabled: bool = False
    db_auto_migrate: bool = True
    metrics_enabled: bool = True
    task_queue_enabled: bool = False
    redis_url: str = "redis://127.0.0.1:6379/0"
    task_queue_dispatch_interval_seconds: float = Field(default=1.0, gt=0, le=30)
    task_queue_batch_size: int = Field(default=20, ge=1, le=100)
    task_queue_max_attempts: int = Field(default=10, ge=1, le=100)
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    llm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_base_url: str = ""
    llm_model: str = ""
    tavily_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    credentials_env_file: str = ".env"
    llm_mode: Literal["local", "remote"] = "local"
    llm_local_base_url: str = ""
    llm_local_model: str = ""
    llm_remote_base_url: str = ""
    llm_remote_model: str = ""
    llm_timeout_seconds: float = Field(default=90, gt=0, le=600)
    llm_max_retries: int = Field(default=2, ge=0, le=3)
    llm_disable_thinking: bool = True
    search_timeout_seconds: float = Field(default=20, gt=0, le=120)
    search_max_retries: int = Field(default=2, ge=0, le=3)
    search_max_queries: int = Field(default=5, ge=1, le=10)
    webpage_timeout_seconds: float = Field(default=20, gt=0, le=120)
    webpage_max_bytes: int = Field(default=3000000, gt=0)
    web_cache_dir: str = "data/cache/web"
    agent_tool_timeout_seconds: float = Field(default=30, gt=0, le=30)
    agent_max_steps: int = Field(default=12, ge=1, le=12)
    agent_max_retrieval_actions: int = Field(default=6, ge=1, le=6)
    agent_max_claim_retrievals: int = Field(default=3, ge=1, le=3)
    agent_max_query_rewrites: int = Field(default=2, ge=0, le=2)
    agent_evidence_max_age_days: int = Field(default=365, ge=1, le=3650)
    agent_lease_seconds: int = Field(default=360, ge=60, le=3600)
    agent_rag_method: Literal["dense", "hybrid", "hybrid_rerank"] = "hybrid"
    document_max_upload_bytes: int = Field(default=20_000_000, gt=0, le=100_000_000)
    document_max_chunk_size: int = Field(default=2_000, ge=1, le=10_000)
    document_max_pages: int = Field(default=1_000, ge=1, le=10_000)
    inference_mode: Literal["local", "remote"] = "local"
    inference_url: str = ""
    inference_token: SecretStr = Field(default=SecretStr(""), repr=False)
    inference_timeout_seconds: float = Field(default=20, gt=0, le=120)
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = "main"
    embedding_manifest_sha256: str = ""
    embedding_device: str = "cpu"
    embedding_use_fp16: bool = False
    embedding_batch_size: int = Field(default=16, gt=0)
    embedding_max_length: int = Field(default=8192, ge=2, le=8192)
    embedding_cache_dir: str = "data/cache/models"
    embedding_local_files_only: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_revision: str = "main"
    reranker_manifest_sha256: str = ""
    reranker_device: str = "cpu"
    reranker_use_fp16: bool = False
    reranker_batch_size: int = Field(default=4, gt=0)
    reranker_max_length: int = Field(default=1024, ge=16, le=8192)
    reranker_cache_dir: str = "data/cache/models"
    reranker_local_files_only: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

settings = Settings()
