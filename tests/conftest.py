import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    def load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text())

    return load


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text()


@pytest.fixture(autouse=True)
def veille_computed_each_time(monkeypatch):
    """Veille recalculée à chaque demande : les tests écrivent en base sans passer par l'API. La Veille préparée
    (main.VEILLE_CACHE) a ses propres tests dans test_app."""
    if "app.main" in sys.modules:
        monkeypatch.setattr(sys.modules["app.main"], "VEILLE_CACHE", False)
    yield
