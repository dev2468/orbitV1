import os

import pytest

from orbit import db

# Constructing an OrbitWindow wires the acknowledgement and speech tracks and
# prewarms both — a real HTTPS request to OpenRouter and a grab of the
# machine's audio output device. Harmless in the app, wrong in a test suite
# that is otherwise offline, and it would happen once per GUI test.
os.environ["ORBIT_DISABLE_PREWARM"] = "1"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_orbit.db")
    db.init_db()
    yield
