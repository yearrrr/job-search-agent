import sqlite3

import pytest

from job_search_agent.db import Database


def test_result_is_persistent_and_idempotent(settings):
    db = Database(settings.data_dir)
    db.initialize()
    assert db.save_smoke_once("same-task", "【虚构】原演示结果") == "【虚构】原演示结果"
    reopened = Database(settings.data_dir)
    reopened.initialize()
    assert reopened.save_smoke_once("same-task", "不同内容不可覆盖") == "【虚构】原演示结果"
    with reopened.connect() as connection:
        assert connection.execute("SELECT count(*) FROM smoke_results").fetchone()[0] == 1
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"smoke_results", "api_attempts", "documents", "facts", "jobs"} <= tables


def test_probe_reservation_and_unknown_usage(settings):
    db = Database(settings.data_dir)
    db.initialize()
    assert db.begin_probe("one")
    assert not db.begin_probe("one")
    assert db.get_probe("one")["prompt_tokens"] is None
    db.finish_probe("one", error_code="timeout", elapsed_seconds=0.1)
    db.finish_probe("one", error_code=None, elapsed_seconds=0.2, total_tokens=99)
    assert db.get_probe("one")["status"] == "failed"
    assert db.get_probe("one")["total_tokens"] is None


def test_transaction_rolls_back_on_failure(settings):
    db = Database(settings.data_dir)
    db.initialize()
    with pytest.raises(RuntimeError):
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO smoke_results(task_id, content) VALUES ('rollback', 'fictional')"
            )
            raise RuntimeError("injected")
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM smoke_results").fetchone()[0] == 0


def test_newer_schema_is_not_overwritten(settings):
    db = Database(settings.data_dir)
    db.initialize()
    with db.connect() as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(sqlite3.DatabaseError):
        db.initialize()
    assert db.health() == 999
