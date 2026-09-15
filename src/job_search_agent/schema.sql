CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('resume','project','notes')),
 corpus TEXT NOT NULL CHECK(corpus IN ('demo','personal')), sha256 TEXT NOT NULL,
 suffix TEXT NOT NULL, byte_size INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('parsed','failed')), error TEXT, created_at TEXT NOT NULL,
 UNIQUE(corpus,kind,sha256)
);
CREATE TABLE IF NOT EXISTS snippets (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 page INTEGER, line INTEGER NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 snippet_id TEXT NOT NULL REFERENCES snippets(id), category TEXT NOT NULL,
 text TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
 method TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 confirmed_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_history (
 id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id), revision INTEGER NOT NULL,
 text TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL, method TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(fact_id,revision)
);
CREATE TABLE IF NOT EXISTS fact_sources (
 fact_id TEXT NOT NULL REFERENCES facts(id), snippet_id TEXT NOT NULL REFERENCES snippets(id),
 ordinal INTEGER NOT NULL, PRIMARY KEY(fact_id,snippet_id), UNIQUE(fact_id,ordinal)
);
INSERT OR IGNORE INTO fact_sources(fact_id,snippet_id,ordinal) SELECT id,snippet_id,0 FROM facts;
CREATE TABLE IF NOT EXISTS preferences (
 corpus TEXT PRIMARY KEY CHECK(corpus IN ('demo','personal')),
 roles TEXT NOT NULL, cities TEXT NOT NULL, notes TEXT NOT NULL, revision INTEGER NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, corpus TEXT NOT NULL CHECK(corpus IN ('demo','personal')),
 company TEXT NOT NULL, title TEXT NOT NULL, job_code TEXT NOT NULL, batch TEXT NOT NULL,
 source_url TEXT NOT NULL, raw_jd TEXT NOT NULL, fingerprint TEXT NOT NULL,
 fields_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL CHECK(status IN ('saved','preparing','applied','interviewing','closed')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(corpus,fingerprint)
);
CREATE TABLE IF NOT EXISTS job_revisions (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), revision INTEGER NOT NULL,
 fields_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(job_id,revision)
);
CREATE TABLE IF NOT EXISTS job_history (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 old_status TEXT, new_status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_notes (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), text TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), text TEXT NOT NULL,
 due_date TEXT, done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
 revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extraction_runs (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 status TEXT NOT NULL CHECK(status IN ('started','ok','failed')), error_code TEXT,
 prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
 elapsed_seconds REAL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snippets_document ON snippets(document_id);
CREATE INDEX IF NOT EXISTS facts_document ON facts(document_id);
CREATE INDEX IF NOT EXISTS jobs_corpus_status ON jobs(corpus,status);
-- 阶段三：业务状态、每次尝试和材料版本独立于 LangGraph 检查点。
CREATE TABLE IF NOT EXISTS agent_tasks (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    mode TEXT NOT NULL CHECK(mode IN ('mock','deepseek')),
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    view_json TEXT NOT NULL DEFAULT '{}',
    wait_id TEXT,
    command_json TEXT,
    command_wait_id TEXT,
    deferred INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_attempts (
    task_id TEXT NOT NULL REFERENCES agent_tasks(id),
    step_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('model','tool')),
    status TEXT NOT NULL CHECK(status IN ('started','ok','failed')),
    request_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    elapsed_seconds REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id,step_key)
);
CREATE TABLE IF NOT EXISTS draft_edits (
    task_id TEXT NOT NULL REFERENCES agent_tasks(id),
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(task_id,revision)
);
CREATE TABLE IF NOT EXISTS material_versions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES agent_tasks(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id,version)
);
-- 重试等待时间独立持久化；旧任务没有 max_retries 时仍保持不重试。
CREATE TABLE IF NOT EXISTS agent_retry_schedule (
    task_id TEXT NOT NULL REFERENCES agent_tasks(id),
    step_key TEXT NOT NULL,
    not_before REAL NOT NULL,
    PRIMARY KEY(task_id,step_key)
);
-- 第二版资料入口：增量表，不修改已有原件、事实和历史。
CREATE TABLE IF NOT EXISTS v2_documents (
 document_id TEXT PRIMARY KEY REFERENCES documents(id),
 format TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_pages (
 document_id TEXT NOT NULL REFERENCES documents(id), page INTEGER NOT NULL,
 snippet_id TEXT NOT NULL REFERENCES snippets(id), image_path TEXT NOT NULL,
 PRIMARY KEY(document_id,page)
);
CREATE TABLE IF NOT EXISTS profile_parse_jobs (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 status TEXT NOT NULL, error_code TEXT, limits_json TEXT NOT NULL,
 result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_profile_parse_active
 ON profile_parse_jobs(document_id) WHERE status IN ('queued','processing');
CREATE TABLE IF NOT EXISTS profile_parse_attempts (
 job_id TEXT NOT NULL REFERENCES profile_parse_jobs(id), attempt INTEGER NOT NULL,
 status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT,
 error_code TEXT, elapsed_seconds REAL, prompt_tokens INTEGER,
 completion_tokens INTEGER, total_tokens INTEGER, created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS profile_entry_details (
 fact_id TEXT PRIMARY KEY REFERENCES facts(id), title TEXT NOT NULL,
 metadata_json TEXT NOT NULL, parse_job_id TEXT REFERENCES profile_parse_jobs(id)
);
CREATE TABLE IF NOT EXISTS profile_entry_history (
 fact_id TEXT NOT NULL REFERENCES facts(id), revision INTEGER NOT NULL,
 title TEXT NOT NULL, metadata_json TEXT NOT NULL, PRIMARY KEY(fact_id,revision)
);
-- 第二批：JD 原件和有界解析 / 准备任务独立于个人履历。
CREATE TABLE IF NOT EXISTS v2_runs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, corpus TEXT NOT NULL,
 target_id TEXT NOT NULL, fingerprint TEXT NOT NULL, mode TEXT NOT NULL,
 status TEXT NOT NULL, error_code TEXT, snapshot_json TEXT NOT NULL,
 limits_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS v2_run_fingerprint ON v2_runs(fingerprint,status);
CREATE TABLE IF NOT EXISTS v2_run_attempts (
 run_id TEXT NOT NULL REFERENCES v2_runs(id), step TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT, error_code TEXT,
 cache_hit INTEGER NOT NULL DEFAULT 0, elapsed_seconds REAL, prompt_tokens INTEGER,
 completion_tokens INTEGER, total_tokens INTEGER, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,step)
);
CREATE TABLE IF NOT EXISTS jd_imports (
 id TEXT PRIMARY KEY, corpus TEXT NOT NULL, fingerprint TEXT NOT NULL,
 input_text TEXT NOT NULL, files_json TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(corpus,fingerprint)
);
CREATE TABLE IF NOT EXISTS jd_confirmations (
 run_id TEXT PRIMARY KEY REFERENCES v2_runs(id), job_id TEXT NOT NULL REFERENCES jobs(id),
 reviewed_json TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_drafts (
 run_id TEXT PRIMARY KEY REFERENCES v2_runs(id), revision INTEGER NOT NULL,
 content_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resume_draft_history (
 run_id TEXT NOT NULL REFERENCES v2_runs(id), revision INTEGER NOT NULL,
 content_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,revision)
);
CREATE TABLE IF NOT EXISTS resume_versions (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES v2_runs(id),
 job_id TEXT NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
 draft_revision INTEGER NOT NULL, content_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 mode TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(job_id,version), UNIQUE(run_id,draft_revision)
);
