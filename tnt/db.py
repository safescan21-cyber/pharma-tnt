"""Storage engine: SQLite tuned for high-throughput OLTP.

Why SQLite: in-process (no network round trip), WAL gives concurrent readers
with one writer, and with the pragmas below it sustains hundreds of thousands
of serial inserts per second on commodity hardware.
"""
from __future__ import annotations

import sqlite3
import threading

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",      # durable at checkpoint; safe with WAL
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=2147483648",    # 2 GiB memory-mapped reads
    "PRAGMA cache_size=-262144",      # 256 MiB page cache per connection
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  gtin TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  strength TEXT,
  dosage_form TEXT,
  manufacturer TEXT,
  ndc TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS locations(
  gln TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- manufacturer | warehouse | distributor | pharmacy | hospital
  address TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS lots(
  gtin TEXT NOT NULL REFERENCES products(gtin),
  lot TEXT NOT NULL,
  expiry TEXT NOT NULL,          -- ISO date
  status TEXT NOT NULL DEFAULT 'released',  -- released | quarantine | recalled
  PRIMARY KEY(gtin, lot)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,     -- canonical: 01<gtin>21<serial> | 00<sscc>
  kind TEXT NOT NULL CHECK(kind IN ('unit','case','pallet')),
  gtin TEXT,
  serial TEXT,
  lot TEXT,
  status TEXT NOT NULL,          -- active | in_transit | dispensed | destroyed | stolen | sample | expired
  location TEXT REFERENCES locations(gln),
  parent_id INTEGER REFERENCES items(id),
  updated_at TEXT NOT NULL,
  FOREIGN KEY(gtin, lot) REFERENCES lots(gtin, lot)
);
CREATE INDEX IF NOT EXISTS ix_items_parent ON items(parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_items_lot ON items(gtin, lot) WHERE gtin IS NOT NULL;

-- EPCIS-style event log, SHA-256 hash-chained for tamper evidence (21 CFR Part 11 / Annex 11)
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  event_type TEXT NOT NULL,      -- ObjectEvent | AggregationEvent
  action TEXT NOT NULL,          -- ADD | OBSERVE | DELETE
  biz_step TEXT NOT NULL,        -- commissioning | packing | shipping | receiving | dispensing | ...
  location TEXT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  data TEXT NOT NULL,            -- canonical JSON
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_items(
  item_id INTEGER NOT NULL,
  event_id INTEGER NOT NULL,
  PRIMARY KEY(item_id, event_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_event_items_event ON event_items(event_id, item_id);

CREATE TABLE IF NOT EXISTS sequences(name TEXT PRIMARY KEY, value INTEGER NOT NULL) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  full_name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('viewer','operator','qa','admin')),
  active INTEGER NOT NULL DEFAULT 1,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  must_change_pw INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  pw_changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  expires_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_events_step ON events(biz_step, id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for p in PRAGMAS:
        conn.execute(p)
    return conn


class Database:
    """One serialized writer connection + one reader connection per thread."""

    def __init__(self, path: str):
        self.path = path
        self.writer = connect(path)
        self.writer.executescript(SCHEMA)
        self.write_lock = threading.Lock()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []

    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            conn.execute("PRAGMA query_only=ON")
            self._local.conn = conn
            self._readers.append(conn)
        return conn

    def close(self) -> None:
        for c in self._readers:
            c.close()
        self.writer.execute("PRAGMA optimize")
        self.writer.close()
