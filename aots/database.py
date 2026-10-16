import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
              trade_id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_price REAL NOT NULL,
              stop_price REAL NOT NULL,
              mark_price REAL NOT NULL,
              quantity INTEGER NOT NULL,
              lots INTEGER NOT NULL,
              status TEXT NOT NULL,
              realised_pnl REAL NOT NULL DEFAULT 0,
              unrealised_pnl REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              event_type TEXT NOT NULL,
              detail_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """)

    def event(self, event_type: str, detail: dict[str, Any]):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(event_type, detail_json) VALUES (?, ?)",
                (event_type, json.dumps(detail, default=str)),
            )

    def get_state(self, key: str, default: Any = None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def ensure_session(self, session_date: date) -> bool:
        """Persist the active calendar date and reset only date-scoped risk state."""
        value = session_date.isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM app_state WHERE key='session_date'"
            ).fetchone()
            if row:
                current_value = json.loads(row["value"])
                if current_value >= value:
                    return False
                conn.executemany(
                    """
                    INSERT INTO app_state(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (
                        ("session_date", json.dumps(value)),
                        ("consecutive_losses", json.dumps(0)),
                        ("paused_for_day", json.dumps(False)),
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO app_state(key, value) VALUES ('session_date', ?)",
                    (json.dumps(value),),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO app_state(key, value) VALUES (?, ?)",
                    (
                        ("consecutive_losses", json.dumps(0)),
                        ("paused_for_day", json.dumps(False)),
                    ),
                )
            return True

    def update_loss_streak(self, session_date: date, pnl: float, limit: int) -> bool:
        """Update the loss streak only if the requested date is still active."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM app_state WHERE key='session_date'"
            ).fetchone()
            if not row or json.loads(row["value"]) != session_date.isoformat():
                return False
            row = conn.execute(
                "SELECT value FROM app_state WHERE key='consecutive_losses'"
            ).fetchone()
            streak = int(json.loads(row["value"])) if row else 0
            streak = streak + 1 if pnl < 0 else 0 if pnl > 0 else streak
            conn.execute(
                """
                INSERT INTO app_state(key, value) VALUES ('consecutive_losses', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (json.dumps(streak),),
            )
            if streak >= limit:
                conn.execute(
                    """
                    INSERT INTO app_state(key, value) VALUES ('paused_for_day', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (json.dumps(True),),
                )
                conn.execute(
                    "INSERT INTO events(event_type, detail_json) VALUES (?, ?)",
                    ("LOSS_STREAK_PAUSE", json.dumps({"consecutive_losses": streak})),
                )
            return True

    def insert_trade(self, trade: dict):
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO trades(trade_id,symbol,side,entry_price,stop_price,mark_price,
                  quantity,lots,status,realised_pnl,unrealised_pnl,created_at,closed_at)
                VALUES (:trade_id,:symbol,:side,:entry_price,:stop_price,:mark_price,
                  :quantity,:lots,:status,:realised_pnl,:unrealised_pnl,:created_at,:closed_at)
            """, trade)

    def update_trade(self, trade: dict):
        with self.connect() as conn:
            conn.execute("""
                UPDATE trades SET mark_price=:mark_price,status=:status,
                  realised_pnl=:realised_pnl,unrealised_pnl=:unrealised_pnl,closed_at=:closed_at
                WHERE trade_id=:trade_id
            """, trade)

    def trades(self):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY created_at DESC").fetchall()]

    def events(self, limit: int = 100):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]
