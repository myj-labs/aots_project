import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any


class IdempotencyConflict(ValueError):
    pass


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
              idempotency_key TEXT UNIQUE,
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
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(trades)")
            }
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE trades ADD COLUMN idempotency_key TEXT")
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_trades_idempotency_key ON trades(idempotency_key)
                    """
                )

    def event(self, event_type: str, detail: dict[str, Any], conn=None):
        if conn is not None:
            conn.execute(
                "INSERT INTO events(event_type, detail_json) VALUES (?, ?)",
                (event_type, json.dumps(detail, default=str)),
            )
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(event_type, detail_json) VALUES (?, ?)",
                (event_type, json.dumps(detail, default=str)),
            )

    def get_state(self, key: str, default: Any = None, conn=None):
        if conn is not None:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row["value"]) if row else default
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value: Any, conn=None):
        if conn is not None:
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def ensure_session(self, session_date: date, conn=None) -> bool:
        """Persist the active calendar date and reset only date-scoped risk state."""
        if conn is None:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.ensure_session(session_date, connection)
        value = session_date.isoformat()
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

    def update_loss_streak(
        self, session_date: date, pnl: float, limit: int, conn=None
    ) -> bool:
        """Update the loss streak only if the requested date is still active."""
        if conn is None:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.update_loss_streak(
                    session_date, pnl, limit, connection
                )
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
            self.event(
                "LOSS_STREAK_PAUSE",
                {"consecutive_losses": streak},
                conn=conn,
            )
        return True

    def insert_trade(self, trade: dict, conn=None):
        if conn is None:
            with self.connect() as connection:
                self.insert_trade(trade, conn=connection)
            return
        conn.execute("""
            INSERT INTO trades(trade_id,idempotency_key,symbol,side,entry_price,stop_price,mark_price,
              quantity,lots,status,realised_pnl,unrealised_pnl,created_at,closed_at)
            VALUES (:trade_id,:idempotency_key,:symbol,:side,:entry_price,:stop_price,:mark_price,
              :quantity,:lots,:status,:realised_pnl,:unrealised_pnl,:created_at,:closed_at)
        """, {**trade, "idempotency_key": trade.get("idempotency_key")})

    def insert_trade_atomically(
        self,
        trade: dict,
        *,
        session_date: date,
        validate,
        idempotency_key: str,
        rejection_type: type[Exception],
    ) -> dict:
        if not idempotency_key.strip():
            raise ValueError(
                "idempotency_key must not be empty or whitespace-only."
            )
        if idempotency_key != idempotency_key.strip():
            raise ValueError(
                "idempotency_key must not have surrounding whitespace."
            )
        rejection = None
        created = False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.ensure_session(session_date, conn)
            existing = conn.execute(
                "SELECT * FROM trades WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                previous = dict(existing)
                comparable_fields = (
                    "symbol", "side", "entry_price", "stop_price",
                    "quantity", "lots",
                )
                if any(previous[field] != trade[field] for field in comparable_fields):
                    raise IdempotencyConflict(
                        "Idempotency key was already used for a different trade."
                    )
                return previous
            try:
                risk_inr = validate(conn)
            except rejection_type as exc:
                rejection = exc
            else:
                self.insert_trade(
                    {**trade, "idempotency_key": idempotency_key},
                    conn=conn,
                )
                self.event(
                    "PAPER_TRADE_OPENED",
                    {
                        "trade_id": trade["trade_id"],
                        "symbol": trade["symbol"],
                        "estimated_risk_inr": risk_inr,
                    },
                    conn=conn,
                )
                created = True
        if rejection is not None:
            raise rejection
        if not created:
            raise RuntimeError("Paper trade was neither created nor replayed.")
        return trade

    def update_trade(self, trade: dict):
        with self.connect() as conn:
            conn.execute("""
                UPDATE trades SET mark_price=:mark_price,status=:status,
                  realised_pnl=:realised_pnl,unrealised_pnl=:unrealised_pnl,closed_at=:closed_at
                WHERE trade_id=:trade_id
            """, trade)

    def transition_open_trade(self, trade: dict, after_transition):
        """Atomically update one open trade and run associated state/event writes."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE trades SET mark_price=:mark_price,status=:status,
                  realised_pnl=:realised_pnl,unrealised_pnl=:unrealised_pnl,
                  closed_at=:closed_at
                WHERE trade_id=:trade_id AND status='OPEN'
                """,
                trade,
            )
            if cursor.rowcount != 1:
                return False
            after_transition(conn)
            return True

    def trades(self, conn=None):
        if conn is not None:
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM trades ORDER BY created_at DESC"
                ).fetchall()
            ]
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY created_at DESC").fetchall()]

    def events(self, limit: int = 100):
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()]
