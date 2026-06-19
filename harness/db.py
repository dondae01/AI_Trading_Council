"""
SQLite persistence layer.  Raw sqlite3 -- no ORM -- so the schema stays
transparent and the file is easy to audit or migrate later.

Schema versioning
-----------------
v1 (original): no leverage, no liquidation columns; direction only allows buy/sell/hold/avoid.
v2 (this):     adds leverage, liquidation_price, liquidated; direction also allows long/short.

migrate() detects the current version by checking for the 'leverage' column and
runs the v1->v2 migration automatically, preserving all existing rows.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from .models import Prediction

DB_PATH = Path(__file__).parent.parent / "data" / "council.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT    NOT NULL,
    track             TEXT    NOT NULL CHECK(track IN ('crypto', 'equities')),
    asset             TEXT    NOT NULL,
    direction         TEXT    NOT NULL
                      CHECK(direction IN ('buy','sell','hold','avoid','long','short')),
    conviction        INTEGER NOT NULL CHECK(conviction BETWEEN 1 AND 10),
    horizon_days      INTEGER NOT NULL,
    horizon_date      TEXT    NOT NULL,
    thesis            TEXT    NOT NULL,
    agents            TEXT    NOT NULL,
    leverage          INTEGER NOT NULL DEFAULT 1 CHECK(leverage >= 1),
    entry_price       REAL,
    liquidation_price REAL,
    resolution_date   TEXT,
    resolution_price  REAL,
    liquidated        INTEGER,
    outcome_correct   INTEGER,
    return_achieved   REAL,
    resolution_notes  TEXT
)
"""


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def migrate() -> None:
    """
    Ensure the DB schema is at v2.  Safe to call on every startup.

    If the table does not exist: creates it fresh with the v2 schema.
    If the table exists without the 'leverage' column (v1): migrates in-place
    by recreating the table and copying all existing rows, setting
    leverage=1 and NULL for the new columns.
    """
    with _conn() as con:
        # Check whether the table exists at all
        table_exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='predictions'"
        ).fetchone()

        if not table_exists:
            con.execute(_CREATE_SQL)
            return

        # Check if we're on v1 (no 'leverage' column)
        col_names = {row["name"] for row in con.execute("PRAGMA table_info(predictions)")}
        if "leverage" in col_names:
            return  # already v2, nothing to do

        # v1 -> v2 migration: recreate table preserving all data
        con.executescript("""
            ALTER TABLE predictions RENAME TO predictions_v1;

            CREATE TABLE predictions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp         TEXT    NOT NULL,
                track             TEXT    NOT NULL CHECK(track IN ('crypto', 'equities')),
                asset             TEXT    NOT NULL,
                direction         TEXT    NOT NULL
                                  CHECK(direction IN ('buy','sell','hold','avoid','long','short')),
                conviction        INTEGER NOT NULL CHECK(conviction BETWEEN 1 AND 10),
                horizon_days      INTEGER NOT NULL,
                horizon_date      TEXT    NOT NULL,
                thesis            TEXT    NOT NULL,
                agents            TEXT    NOT NULL,
                leverage          INTEGER NOT NULL DEFAULT 1 CHECK(leverage >= 1),
                entry_price       REAL,
                liquidation_price REAL,
                resolution_date   TEXT,
                resolution_price  REAL,
                liquidated        INTEGER,
                outcome_correct   INTEGER,
                return_achieved   REAL,
                resolution_notes  TEXT
            );

            INSERT INTO predictions
                (id, timestamp, track, asset, direction, conviction,
                 horizon_days, horizon_date, thesis, agents,
                 leverage, entry_price, liquidation_price,
                 resolution_date, resolution_price, liquidated,
                 outcome_correct, return_achieved, resolution_notes)
            SELECT
                id, timestamp, track, asset, direction, conviction,
                horizon_days, horizon_date, thesis, agents,
                1, entry_price, NULL,
                resolution_date, resolution_price, NULL,
                outcome_correct, return_achieved, resolution_notes
            FROM predictions_v1;

            DROP TABLE predictions_v1;
        """)


# ------------------------------------------------------------------
# Write operations
# ------------------------------------------------------------------

def insert_prediction(p: Prediction) -> int:
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO predictions
              (timestamp, track, asset, direction, conviction,
               horizon_days, horizon_date, thesis, agents,
               leverage, entry_price, liquidation_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p.timestamp.isoformat(),
                p.track,
                p.asset,
                p.direction,
                p.conviction,
                p.horizon_days,
                p.horizon_date.isoformat(),
                p.thesis,
                json.dumps(p.agents),
                p.leverage,
                p.entry_price,
                p.liquidation_price,
            ),
        )
        return cur.lastrowid


def update_resolution(
    prediction_id: int,
    resolution_date: datetime,
    resolution_price: float,
    liquidated: Optional[bool],
    outcome_correct: bool,
    return_achieved: Optional[float],
    resolution_notes: str = "",
) -> None:
    with _conn() as con:
        con.execute(
            """
            UPDATE predictions SET
                resolution_date   = ?,
                resolution_price  = ?,
                liquidated        = ?,
                outcome_correct   = ?,
                return_achieved   = ?,
                resolution_notes  = ?
            WHERE id = ?
            """,
            (
                resolution_date.isoformat(),
                resolution_price,
                (1 if liquidated else 0) if liquidated is not None else None,
                1 if outcome_correct else 0,
                return_achieved,
                resolution_notes,
                prediction_id,
            ),
        )


# ------------------------------------------------------------------
# Read operations
# ------------------------------------------------------------------

def get_prediction(prediction_id: int) -> Optional[Prediction]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM predictions WHERE id = ?", (prediction_id,)
        ).fetchone()
    return _row_to_prediction(row) if row else None


def get_predictions(
    track: Optional[str] = None,
    resolved_only: bool = False,
    pending_only: bool = False,
) -> List[Prediction]:
    clauses: List[str] = []
    params: List = []
    if track:
        clauses.append("track = ?")
        params.append(track)
    if resolved_only:
        clauses.append("outcome_correct IS NOT NULL")
    if pending_only:
        clauses.append("outcome_correct IS NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM predictions {where} ORDER BY timestamp", params
        ).fetchall()
    return [_row_to_prediction(r) for r in rows]


def get_due_predictions() -> List[Prediction]:
    """All unresolved predictions whose horizon_date has passed."""
    today = date.today().isoformat()
    with _conn() as con:
        rows = con.execute(
            """
            SELECT * FROM predictions
            WHERE outcome_correct IS NULL AND horizon_date <= ?
            ORDER BY horizon_date
            """,
            (today,),
        ).fetchall()
    return [_row_to_prediction(r) for r in rows]


# ------------------------------------------------------------------
# Internal
# ------------------------------------------------------------------

def _row_to_prediction(row) -> Prediction:
    return Prediction(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        track=row["track"],
        asset=row["asset"],
        direction=row["direction"],
        conviction=row["conviction"],
        horizon_days=row["horizon_days"],
        horizon_date=date.fromisoformat(row["horizon_date"]),
        thesis=row["thesis"],
        agents=json.loads(row["agents"]),
        leverage=row["leverage"],
        entry_price=row["entry_price"],
        liquidation_price=row["liquidation_price"],
        resolution_date=(
            datetime.fromisoformat(row["resolution_date"])
            if row["resolution_date"]
            else None
        ),
        resolution_price=row["resolution_price"],
        liquidated=(
            bool(row["liquidated"]) if row["liquidated"] is not None else None
        ),
        outcome_correct=(
            bool(row["outcome_correct"]) if row["outcome_correct"] is not None else None
        ),
        return_achieved=row["return_achieved"],
        resolution_notes=row["resolution_notes"],
    )
