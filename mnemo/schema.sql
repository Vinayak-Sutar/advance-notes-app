-- Mnemo index. This database is a CACHE: every row here can be rebuilt by
-- re-reading the Markdown vault (see vault.rebuild). The .md files are truth.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- notes ----
CREATE TABLE IF NOT EXISTS notes (
    id            TEXT PRIMARY KEY,          -- ULID, stable forever
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'note',   -- link|video|article|image|task|idea|reference|snippet|note
    status        TEXT NOT NULL DEFAULT 'open',   -- open|done|archived
    path          TEXT,                      -- vault-relative, e.g. Technology/AI/rag.md
    source_url    TEXT,
    media_path    TEXT,
    content_hash  TEXT,
    needs_review  INTEGER NOT NULL DEFAULT 0,
    due_at        TEXT,                      -- ISO time this is due / should fire
    remind_at     TEXT,                      -- when the notification fires
    reminded      INTEGER NOT NULL DEFAULT 0,--fired already, so it does not repeat
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_kind    ON notes(kind);
CREATE INDEX IF NOT EXISTS idx_notes_status  ON notes(status);
CREATE INDEX IF NOT EXISTS idx_notes_review  ON notes(needs_review) WHERE needs_review = 1;
CREATE INDEX IF NOT EXISTS idx_notes_due     ON notes(due_at) WHERE due_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notes_remind  ON notes(remind_at) WHERE remind_at IS NOT NULL AND reminded = 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_path ON notes(path) WHERE path IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
    USING fts5(title, body, content='notes', content_rowid='rowid', tokenize='porter unicode61');

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, title, body) VALUES('delete', old.rowid, old.title, old.body);
    INSERT INTO notes_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

-- Your own order of notes (dragging tiles). Higher comes first. A note with no
-- row sorts by when it was made, so new notes land on top. Mirrors the
-- `order:` line in each file's header.
CREATE TABLE IF NOT EXISTS note_order (
    note_id TEXT PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    pos     REAL NOT NULL
);

-- To-do list settings from each file's header, so the lists view and the
-- reminder checks don't have to open every file. A row only when one is set.
CREATE TABLE IF NOT EXISTS note_meta (
    note_id     TEXT PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    repeat      TEXT,
    checkin     TEXT,
    quiet_until TEXT,
    parent      TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_meta_parent ON note_meta(parent) WHERE parent IS NOT NULL;

-- What the reminder checks have already shown (check-ins, deadlines, items),
-- so each fires once. Keys look like "checkin:<id>:2026-10-28".
CREATE TABLE IF NOT EXISTS reminder_log (
    key      TEXT PRIMARY KEY,
    fired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_vec (
    note_id   TEXT PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,       -- little-endian float32, L2-normalised
    dim       INTEGER NOT NULL,
    model     TEXT NOT NULL
);

-- --------------------------------------------------------------- topics ----
CREATE TABLE IF NOT EXISTS topics (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES topics(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    norm_key    TEXT NOT NULL DEFAULT '',   -- normal_key(name); duplicate detection
    name_vec    BLOB,                        -- embedding of the NAME (not the notes)
    description TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'provisional',  -- provisional|active|dissolved
    note_count  INTEGER NOT NULL DEFAULT 0,
    depth       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_parent ON topics(parent_id);
CREATE INDEX IF NOT EXISTS idx_topics_state  ON topics(state);
CREATE INDEX IF NOT EXISTS idx_topics_normkey ON topics(norm_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_sibling_slug
    ON topics(IFNULL(parent_id, '~root~'), slug) WHERE state <> 'dissolved';

CREATE TABLE IF NOT EXISTS topic_vec (
    topic_id TEXT PRIMARY KEY REFERENCES topics(id) ON DELETE CASCADE,
    centroid BLOB NOT NULL,
    dim      INTEGER NOT NULL,
    n        INTEGER NOT NULL DEFAULT 0     -- notes folded into this centroid
);

-- "videogames" -> t_gaming. Populated by the gate every time it refuses a
-- near-duplicate topic, so the same synonym resolves instantly next time.
CREATE TABLE IF NOT EXISTS topic_aliases (
    alias      TEXT PRIMARY KEY,
    topic_id   TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_topics (
    note_id     TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    topic_id    TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    confidence  REAL NOT NULL DEFAULT 0.0,
    assigned_by TEXT NOT NULL DEFAULT 'llm',   -- llm|user|gardener
    PRIMARY KEY (note_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_note_topics_topic ON note_topics(topic_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_note_one_primary
    ON note_topics(note_id) WHERE is_primary = 1;

-- ------------------------------------------------- entities and edges ----
CREATE TABLE IF NOT EXISTS entities (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL DEFAULT 'thing',
    canonical_id TEXT REFERENCES entities(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS note_entities (
    note_id   TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (note_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_note_entities_entity ON note_entities(entity_id);

CREATE TABLE IF NOT EXISTS edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    rel        TEXT NOT NULL,               -- related|follows|duplicate
    weight     REAL NOT NULL DEFAULT 1.0,
    created_by TEXT NOT NULL DEFAULT 'system',
    PRIMARY KEY (src, dst, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

-- ------------------------------------------- journal / proposals / log ----
-- Every mutation stores its own inverse, so any action is undoable later.
CREATE TABLE IF NOT EXISTS journal (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    op      TEXT NOT NULL,
    payload TEXT NOT NULL,      -- JSON
    inverse TEXT NOT NULL,      -- JSON
    undone  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS proposals (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,   -- merge|split|reparent|dissolve|refile
    payload    TEXT NOT NULL,   -- JSON
    score      REAL NOT NULL DEFAULT 0.0,
    state      TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|dismissed
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state);

-- The learning signal: what we offered, what was chosen, what you corrected
-- it to. Feeds few-shot exemplars and centroid nudging.
CREATE TABLE IF NOT EXISTS routing_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id          TEXT NOT NULL,
    candidates       TEXT NOT NULL,   -- JSON [{topic_id, sim, note_count}]
    chosen           TEXT,
    action           TEXT NOT NULL,
    confidence       REAL NOT NULL DEFAULT 0.0,
    outcome          TEXT NOT NULL,   -- filed|review|inbox
    user_corrected_to TEXT,
    ts               TEXT NOT NULL
);
