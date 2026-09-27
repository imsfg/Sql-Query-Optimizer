import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.graph import SQLAgent  # noqa: E402
from app.agent.nodes import Deps  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import SchemaCatalog  # noqa: E402
from data.seed import build_database  # noqa: E402


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    return build_database(tmp_path_factory.mktemp("db") / "company.db")


@pytest.fixture(scope="session")
def settings(db_path, tmp_path_factory) -> Settings:
    return Settings(db_path=db_path, checkpoint_db_path=tmp_path_factory.mktemp("ckpt") / "checkpoints.db",
                    llm_refusal_fallback=False)


@pytest.fixture(scope="session")
def catalog(db_path) -> SchemaCatalog:
    return SchemaCatalog.from_sqlite(db_path)


@pytest.fixture
def make_agent(settings, catalog):
    def _make(llm):
        return SQLAgent(Deps(settings, catalog, llm), InMemorySaver())
    return _make
