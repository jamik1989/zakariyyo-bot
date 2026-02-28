# app/db.py
import sqlite3
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List

DB_PATH = Path(__file__).parent / "storage" / "app.db"


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # operators
    cur.execute("""
    CREATE TABLE IF NOT EXISTS operators (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        password TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # confirms
    cur.execute("""
    CREATE TABLE IF NOT EXISTS confirms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operator_id INTEGER NOT NULL,
        brand TEXT NOT NULL,
        client_name TEXT DEFAULT '',
        phone_plus TEXT DEFAULT '',
        counterparty_meta TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        created_at TEXT DEFAULT (datetime('now')),
        done_at TEXT DEFAULT NULL,
        FOREIGN KEY(operator_id) REFERENCES operators(id)
    )
    """)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_confirms_operator_status "
        "ON confirms(operator_id, status)"
    )

    conn.commit()
    conn.close()

    # ✅ Railway ENV dan operatorlarni seed qilish
    seed_operators_from_env()


# ---------------- OPERATORS SEED ----------------

def seed_operators_from_env() -> int:
    """
    Railway ENV format:
    OPERATORS_SEED=phone,name,password;phone,name,password;...

    Misol:
    935083009,birkachi,3009;979924747,birkachi1,3421
    """
    raw = (os.getenv("OPERATORS_SEED") or "").strip()
    if not raw:
        return 0

    added = 0
    items = [x.strip() for x in raw.split(";") if x.strip()]

    for item in items:
        cols = [c.strip() for c in item.split(",")]
        if len(cols) < 3:
            continue

        phone, name, password = cols[0], cols[1], cols[2]

        # faqat raqamlarni qoldiramiz
        phone = "".join(ch for ch in phone if ch.isdigit())
        if not phone:
            continue

        ok = create_operator(phone, name, password)
        if ok:
            added += 1

    return added


# ---------------- operators ----------------

def create_operator(phone: str, name: str, password: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO operators (phone, name, password) VALUES (?, ?, ?)",
            ((phone or "").strip(), (name or "").strip(), (password or "").strip())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def check_operator(phone: str, password: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, phone, name FROM operators WHERE phone=? AND password=?",
        ((phone or "").strip(), (password or "").strip())
    )
    row = cur.fetchone()
    conn.close()
    return row


# ---------------- confirms ----------------

def create_confirm(
    operator_id: int,
    brand: str,
    client_name: str,
    phone_plus: str,
    counterparty_meta: Dict[str, Any],
) -> int:

    conn = get_conn()
    cur = conn.cursor()

    meta_json = json.dumps(counterparty_meta or {}, ensure_ascii=False)

    cur.execute(
        """
        INSERT INTO confirms (operator_id, brand, client_name, phone_plus, counterparty_meta, status)
        VALUES (?, ?, ?, ?, ?, 'OPEN')
        """,
        (
            int(operator_id),
            (brand or "").strip(),
            (client_name or "").strip(),
            (phone_plus or "").strip(),
            meta_json,
        ),
    )
    conn.commit()
    new_id = int(cur.lastrowid)
    conn.close()
    return new_id


def create_confirm_upsert(
    operator_id: int,
    brand: str,
    client_name: str,
    phone_plus: str,
    counterparty_meta: Dict[str, Any],
) -> int:

    op_id = int(operator_id or 0)
    brand_key = (brand or "").strip().upper()
    phone_key = (phone_plus or "").strip()
    client_clean = (client_name or "").strip()
    meta_json = json.dumps(counterparty_meta or {}, ensure_ascii=False)

    if not op_id or not brand_key or not phone_key:
        return create_confirm(operator_id, brand, client_name, phone_plus, counterparty_meta)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id
        FROM confirms
        WHERE operator_id=?
          AND status='OPEN'
          AND upper(trim(brand))=?
          AND trim(phone_plus)=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (op_id, brand_key, phone_key),
    )
    row = cur.fetchone()

    if row:
        existing_id = int(row["id"])
        cur.execute(
            """
            UPDATE confirms
            SET client_name=?,
                counterparty_meta=?
            WHERE operator_id=? AND id=? AND status='OPEN'
            """,
            (client_clean, meta_json, op_id, existing_id),
        )
        conn.commit()
        conn.close()
        return existing_id

    conn.close()
    return create_confirm(op_id, brand_key, client_clean, phone_key, counterparty_meta)


def list_open_confirms(operator_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, brand, client_name, phone_plus, counterparty_meta, created_at
        FROM confirms
        WHERE operator_id=? AND status='OPEN'
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(operator_id), int(limit)),
    )
    rows = cur.fetchall()
    conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": int(r["id"]),
            "brand": r["brand"],
            "client_name": r["client_name"],
            "phone_plus": r["phone_plus"],
            "counterparty_meta": json.loads(r["counterparty_meta"] or "{}"),
            "created_at": r["created_at"],
        })
    return out


def mark_confirm_done(operator_id: int, confirm_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE confirms
        SET status='DONE', done_at=datetime('now')
        WHERE operator_id=? AND id=? AND status='OPEN'
        """,
        (int(operator_id), int(confirm_id)),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ---------------- admin helpers ----------------

def list_operators(limit: int = 200) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, phone, name, created_at FROM operators ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": int(r["id"]), "phone": r["phone"], "name": r["name"], "created_at": r["created_at"]}
        for r in rows
    ]


def count_operators() -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) AS c FROM operators")
    row = cur.fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def delete_operator_by_phone(phone: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM operators WHERE phone=?", ((phone or "").strip(),))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted