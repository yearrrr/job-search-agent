from ..schemas import Conflict, bounded, valid_corpus
from .common import now, once
from .documents import attach_sources


class Profile:
    def __init__(self, database):
        self.db = database

    def confirmed(self, corpus="personal"):
        valid_corpus(corpus)
        with self.db.connect() as connection:
            return [
                attach_sources(connection, dict(row))
                for row in connection.execute(
                    """SELECT f.*,d.name,s.page,s.line,s.text AS quote FROM facts f
                   JOIN documents d ON d.id=f.document_id JOIN snippets s ON s.id=f.snippet_id
                   WHERE f.status='confirmed' AND d.corpus=? AND d.kind IN ('resume','project')
                   ORDER BY f.category,d.created_at,s.page,s.line""",
                    (corpus,),
                )
            ]

    def preferences(self, corpus):
        valid_corpus(corpus)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM preferences WHERE corpus=?", (corpus,)
            ).fetchone()
            return dict(row) if row else {"roles": "", "cities": "", "notes": "", "revision": 0}

    def set_preferences(self, corpus, roles, cities, notes, revision, operation_id):
        valid_corpus(corpus)
        roles, cities, notes = (
            bounded(v, n, empty=True) for v, n in [(roles, 500), (cities, 500), (notes, 2000)]
        )
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def save():
                previous = connection.execute(
                    "SELECT revision FROM preferences WHERE corpus=?", (corpus,)
                ).fetchone()
                if (previous["revision"] if previous else 0) != revision:
                    raise Conflict("偏好已被更新，请刷新页面。")
                connection.execute(
                    """INSERT INTO preferences VALUES (?,?,?,?,?,?) ON CONFLICT(corpus)
                    DO UPDATE SET roles=excluded.roles,cities=excluded.cities,notes=excluded.notes,
                    revision=excluded.revision,updated_at=excluded.updated_at""",
                    (corpus, roles, cities, notes, revision + 1, now()),
                )
                return {"corpus": corpus}

            return once(
                connection,
                operation_id,
                ["preferences", corpus, roles, cities, notes, revision],
                save,
            )
