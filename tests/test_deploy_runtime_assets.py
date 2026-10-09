from pathlib import Path


def test_sandbox_stage_copies_version_module_when_config_imports_it():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY app/__init__.py app/config.py app/security.py app/schemas.py app/version.py ./app/" in dockerfile
