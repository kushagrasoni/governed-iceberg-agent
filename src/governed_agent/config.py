"""Paths and catalog wiring. Everything lives under one local 'home' directory."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.catalog.sql import SqlCatalog


@dataclass(frozen=True)
class Settings:
    home: Path

    @property
    def warehouse(self) -> Path:
        return self.home / "warehouse"

    @property
    def catalog_db(self) -> Path:
        return self.home / "catalog.db"

    @property
    def audit_path(self) -> Path:
        return self.home / "audit" / "audit.jsonl"

    @property
    def plans_dir(self) -> Path:
        return self.home / "plans"

    @property
    def policy_path(self) -> Path:
        return self.home / "policy.json"

    def ensure(self) -> "Settings":
        for p in (self.warehouse, self.audit_path.parent, self.plans_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_settings(home: str | os.PathLike | None = None) -> Settings:
    root = Path(home or os.environ.get("GIA_HOME", ".lakehouse")).resolve()
    return Settings(home=root).ensure()


def get_catalog(settings: Settings) -> Catalog:
    """Default: a SQLite-backed Iceberg SQL catalog on the local filesystem (no cloud keys).

    Set GIA_CATALOG_NAME to load a catalog defined in ~/.pyiceberg.yaml instead
    (e.g. a REST catalog such as Apache Polaris). That path is not exercised by the tests.
    """
    name = os.environ.get("GIA_CATALOG_NAME")
    if name:
        return load_catalog(name)
    return SqlCatalog(
        "local",
        uri=f"sqlite:///{settings.catalog_db}",
        warehouse=settings.warehouse.as_uri(),
    )


def load_policy(settings: Settings | None = None) -> dict:
    """Policy is loaded from <home>/policy.json if present, else the packaged default."""
    if settings is not None and settings.policy_path.exists():
        return json.loads(settings.policy_path.read_text())
    text = resources.files("governed_agent").joinpath("default_policy.json").read_text()
    return json.loads(text)
