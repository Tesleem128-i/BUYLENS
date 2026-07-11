"""
One-off migration: adds the reset_token / reset_token_expiry columns to an
existing buylens.db that predates the forgot-password feature.

Usage (from the same folder as app.py / buylens.db):
    python migrate_add_reset_token.py

Safe to run more than once — it checks whether each column already exists
before trying to add it.
"""
import sqlite3
import sys

DB_PATH = "buylens.db"


def column_exists(cursor, table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def main():
    try:
        conn = sqlite3.connect(DB_PATH)
    except sqlite3.Error as exc:
        print(f"Couldn't open {DB_PATH}: {exc}")
        sys.exit(1)

    cur = conn.cursor()

    added = []
    if not column_exists(cur, "user", "reset_token"):
        cur.execute("ALTER TABLE user ADD COLUMN reset_token VARCHAR(128)")
        added.append("reset_token")

    if not column_exists(cur, "user", "reset_token_expiry"):
        cur.execute("ALTER TABLE user ADD COLUMN reset_token_expiry DATETIME")
        added.append("reset_token_expiry")

    conn.commit()
    conn.close()

    if added:
        print(f"✅ Added columns: {', '.join(added)}")
    else:
        print("✅ Nothing to do — both columns already exist.")


if __name__ == "__main__":
    main()