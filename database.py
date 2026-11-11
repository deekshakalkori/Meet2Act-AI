import json
import sqlite3

DB = "meet2act.db"


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meetings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            location TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            summary TEXT,
            decisions TEXT);
        CREATE TABLE IF NOT EXISTS actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER,
            task TEXT NOT NULL,
            owner TEXT,
            due TEXT,
            status TEXT DEFAULT 'todo');
        """)


def create_user(name, email, pw_hash):
    with conn() as c:
        return c.execute("INSERT INTO users(name, email, password_hash) VALUES(?,?,?)",
                         (name, email, pw_hash)).lastrowid


def get_user(email):
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(r) if r else None


def find_conflict(location, start, end):
    """Same location (ignoring case/spaces) and overlapping time = already booked."""
    with conn() as c:
        r = c.execute(
            "SELECT title, start_time, end_time FROM meetings "
            "WHERE lower(trim(location)) = lower(trim(?)) AND start_time < ? AND end_time > ?",
            (location, end, start)).fetchone()
    return dict(r) if r else None


def add_meeting(user_id, title, location, start, end, summary, decisions, actions):
    with conn() as c:
        mid = c.execute(
            "INSERT INTO meetings(user_id, title, location, start_time, end_time, summary, decisions) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, title, location.strip(), start, end, summary, json.dumps(decisions))).lastrowid
        for a in actions:
            c.execute("INSERT INTO actions(meeting_id, task, owner, due) VALUES(?,?,?,?)",
                      (mid, a.get("task"), a.get("owner"), a.get("due")))
    return mid


def list_meetings():
    with conn() as c:
        rows = c.execute(
            "SELECT m.*, u.name AS organizer, "
            "(SELECT COUNT(*) FROM actions a WHERE a.meeting_id = m.id) AS n_actions "
            "FROM meetings m LEFT JOIN users u ON u.id = m.user_id "
            "ORDER BY m.start_time DESC").fetchall()
    out = []
    for r in rows:
        m = dict(r)
        m["decisions"] = json.loads(m["decisions"] or "[]")
        out.append(m)
    return out


def list_actions():
    with conn() as c:
        rows = c.execute(
            "SELECT a.*, m.title AS meeting FROM actions a "
            "JOIN meetings m ON m.id = a.meeting_id ORDER BY a.id DESC").fetchall()
    return [dict(r) for r in rows]


def set_status(action_id, status):
    with conn() as c:
        c.execute("UPDATE actions SET status=? WHERE id=?", (status, action_id))


def delete_meeting(mid, user_id):
    with conn() as c:
        row = c.execute("SELECT user_id FROM meetings WHERE id=?", (mid,)).fetchone()
        if not row or row["user_id"] != user_id:
            return False
        c.execute("DELETE FROM actions WHERE meeting_id=?", (mid,))
        c.execute("DELETE FROM meetings WHERE id=?", (mid,))
    return True


if __name__ == "__main__":
    init_db()
    print("Meet2Act database ready")