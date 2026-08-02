from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker" / "docker-compose.neo4j-retrieval.yml"
ENV_EXAMPLE_PATH = REPO_ROOT / "config" / "retrieval.env.example"
DOCKERFILE_PATH = REPO_ROOT / "docker" / "retrieval-api.Dockerfile"
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_e2e_services_are_bound_to_loopback_only():
    services = _compose()["services"]
    assert all(
        str(binding).startswith("127.0.0.1:")
        for service_name in ("neo4j", "retrieval-api")
        for binding in services[service_name]["ports"]
    )


def test_neo4j_does_not_enable_apoc_or_gds_by_default():
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8").lower()
    env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8").lower()
    assert "apoc.*" not in compose_text
    assert "gds.*" not in compose_text
    assert "neo4j_plugins=[]" in env_example


def test_neo4j_healthcheck_does_not_trigger_early_authentication_lockout():
    healthcheck = _compose()["services"]["neo4j"]["healthcheck"]["test"]
    assert "wget" in " ".join(healthcheck)
    assert "cypher-shell" not in " ".join(healthcheck)


def test_api_receives_cascade_and_embedding_provider_configuration():
    environment = _compose()["services"]["retrieval-api"]["environment"]
    assert "K12_ROUTER_MODE" in environment
    assert "K12_ROUTER_API_KEY" in environment
    assert "K12_RETRIEVAL_EMBEDDING_PROVIDER" in environment
    assert "K12_RETRIEVAL_EMBEDDING_API_KEY" in environment
    assert "K12_RETRIEVAL_EMBEDDING_DIMENSION" in environment
    assert "K12_CORS_ORIGINS" in environment
    assert "K12_NEO4J_POOL_WARMUP_CONCURRENCY" in environment


def test_example_environment_contains_no_real_api_keys():
    values = {}
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    assert values["DEEPSEEK_API_KEY"] == ""
    assert values["EMBEDDING_API_KEY"] == ""
    assert values["NEO4J_PORT"] != "7687"
    assert values["NEO4J_HTTP_PORT"] != "7474"
    assert values["RETRIEVAL_API_PORT"] != "8000"


def test_api_image_installs_locked_runtime_dependencies_from_uv_lock():
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "pip install -r" not in dockerfile


def test_docker_context_excludes_private_and_generated_files():
    ignored = {
        line.strip()
        for line in DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        ".git",
        ".venv",
        ".omx",
        ".codex",
        ".superpowers",
        "config/retrieval.env",
        "eval/retrieval/results/",
    } <= ignored
