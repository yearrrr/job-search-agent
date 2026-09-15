"""版本化业务库。升级保留已有原件、探测和图演示记录。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 7


class Database:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.path = self.data_dir / "app.sqlite3"

    def local_path(self, *parts: str) -> Path:
        path = self.data_dir.joinpath(*parts).resolve()
        if not path.is_relative_to(self.data_dir):
            raise OSError("本地资料路径超出配置目录。")
        return path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.local_path("app.sqlite3"), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, SCHEMA_VERSION):
                raise sqlite3.DatabaseError("不支持此数据库版本，请保留数据并使用匹配的程序版本。")
            # executescript 自行提交；显式 BEGIN/COMMIT 保证迁移的所有 DDL 原子生效。
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS smoke_results (
                    task_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
                CREATE TABLE IF NOT EXISTS api_attempts (
                    probe_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('started', 'ok', 'failed')),
                    error_code TEXT,
                    elapsed_seconds REAL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                );
            """
                + Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                + """
                PRAGMA user_version = 7;
                COMMIT;
            """
            )

    def health(self) -> int:
        with self.connect() as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    def save_smoke_once(self, task_id: str, content: str) -> str:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO smoke_results(task_id, content) VALUES (?, ?) ON CONFLICT(task_id) DO NOTHING",
                (task_id, content),
            )
            return connection.execute(
                "SELECT content FROM smoke_results WHERE task_id = ?", (task_id,)
            ).fetchone()[0]

    def begin_probe(self, probe_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO api_attempts(probe_id, status) VALUES (?, 'started') ON CONFLICT(probe_id) DO NOTHING",
                (probe_id,),
            )
            return cursor.rowcount == 1

    def finish_probe(
        self,
        probe_id: str,
        *,
        error_code: str | None,
        elapsed_seconds: float,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE api_attempts SET status=?, error_code=?, elapsed_seconds=?,
                prompt_tokens=?, completion_tokens=?, total_tokens=? WHERE probe_id=? AND status='started'""",
                (
                    "failed" if error_code else "ok",
                    error_code,
                    elapsed_seconds,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    probe_id,
                ),
            )

    def get_probe(self, probe_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_attempts WHERE probe_id=?", (probe_id,)
            ).fetchone()
            return dict(row) if row else None
