"""Hosting: the files Vercel reads, and behaviour on a read-only serverless host."""

import ast
import base64
import importlib
import json
import os
import tomllib
from pathlib import Path

import pytest

from munim.config import get_settings
from munim.crypto import load_key
from munim.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent


def _requirement_lines():
    return sorted(line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
                  if line.strip() and not line.startswith("#"))


def test_pyproject_dependencies_match_requirements_txt():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert sorted(project["dependencies"]) == _requirement_lines()


def test_vercel_entrypoint_names_the_fastapi_app():
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["vercel"]
    module, attr = tool["entrypoint"].split(":")
    assert (ROOT / (module.replace(".", "/") + ".py")).exists()   # what Vercel checks
    from fastapi import FastAPI
    assert isinstance(getattr(importlib.import_module(module), attr), FastAPI)


def test_vercel_config_is_valid_json_with_one_region():
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert len(cfg["regions"]) == 1                     # the Hobby plan allows one


def test_vercelignore_keeps_secrets_and_the_local_ledger_out():
    lines = (ROOT / ".vercelignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines and "data/" in lines


def test_code_parses_as_python_3_12():
    """Vercel may run an older Python than the one used for development."""
    for path in (ROOT / "munim").glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 12))


# -------------------------------------------------------------- storage
@pytest.fixture
def clean_env(monkeypatch):
    for name in ("VERCEL", "AWS_LAMBDA_FUNCTION_NAME", "DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_on_vercel_the_ledger_goes_to_a_writable_temp_folder(clean_env, tmp_path):
    clean_env.setenv("VERCEL", "1")
    clean_env.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    s = get_settings()
    assert s.ephemeral and s.data_dir == tmp_path / "munim"


def test_an_explicit_data_dir_wins_and_counts_as_disk(clean_env, tmp_path):
    clean_env.setenv("VERCEL", "1")
    clean_env.setenv("DATA_DIR", str(tmp_path / "khata"))
    s = get_settings()
    assert not s.ephemeral and s.data_dir == tmp_path / "khata"


def test_locally_the_ledger_stays_in_the_repo_data_folder(clean_env):
    s = get_settings()
    assert not s.ephemeral and s.data_dir == ROOT / "data"


# ------------------------------------------------------------------ keys
def test_a_key_pasted_without_its_padding_is_accepted(tmp_path):
    raw = os.urandom(32)
    pasted = base64.b64encode(raw).decode().rstrip("=")
    assert len(pasted) == 43 and load_key(pasted, tmp_path) == raw


@pytest.mark.parametrize("bad", ["not a key", "c2hvcnQ=", "%%%%"])
def test_a_wrong_key_explains_how_to_make_one(bad, tmp_path):
    with pytest.raises(ValueError, match="44 characters"):
        load_key(bad, tmp_path)


def test_a_ledger_opened_with_another_key_says_so(tmp_path):
    db = tmp_path / "khata.db"
    Ledger(db, os.urandom(32)).add_customer("Suresh", "सुरेश")
    with pytest.raises(RuntimeError, match="does not open this ledger"):
        Ledger(db, os.urandom(32))
