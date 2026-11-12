import io
import json
import os
import re
from datetime import date
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

import database as db

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
db.init_db()

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

PROMPT = """Today is {today}. Read this meeting transcript and reply with ONLY JSON:
{{"summary": "2-3 sentence summary",
  "decisions": ["decision", ...],
  "actions": [{{"task": "...", "owner": "name or null", "due": "YYYY-MM-DD or null"}}]}}
Only include actions someone clearly committed to or was assigned.

Transcript:
{transcript}"""


def ask_claude(prompt, max_tokens=1500):
    import anthropic  # reads ANTHROPIC_API_KEY from the environment
    msg = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text


def analyze_with_ai(transcript):
    text = ask_claude(PROMPT.format(today=date.today().isoformat(), transcript=transcript))
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


def analyze_basic(transcript):
    """Offline fallback: no AI, just keyword matching."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", transcript.strip())
    actions = []
    for s in sentences:
        m = re.search(r"\b([A-Z][a-z]+)\s+(?:will|to|should|needs to)\s+(.+)", s)
        if m or re.search(r"action item|todo|to-do", s, re.I):
            actions.append({"task": s.strip(), "owner": m.group(1) if m else None, "due": None})
    return {"summary": " ".join(sentences[:2]), "decisions": [], "actions": actions}


def booked_message(location, c):
    return (f'Already booked: {location} is taken by "{c["title"]}" from {c["start_time"][11:]} '
            f'to {c["end_time"][11:]} on {c["start_time"][:10]}. Please choose another location or time.')


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "uid" not in session:
            if request.path.startswith("/api/"):
                return jsonify(error="Please sign in."), 401
            return redirect("/login")
        return f(*a, **kw)
    return wrapper


@app.after_request
def cors(resp):
    o = request.headers.get("Origin", "")
    if re.match(r"^http://(127\.0\.0\.1|localhost):\d+$", o):
        resp.headers["Access-Control-Allow-Origin"] = o
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    import traceback
    traceback.print_exc()
    if request.path.startswith("/api/"):
        return jsonify(error=f"Server error: {e}"), 500
    raise e


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    if "uid" in session:
        return redirect("/")
    return render_template("login.html")


@app.post("/api/register")
def register():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify(error="Enter a valid email address."), 400
    if len(pw) < 6:
        return jsonify(error="Password must be at least 6 characters."), 400
    try:
        uid = db.create_user(name or email.split("@")[0].title(), email,
                             generate_password_hash(pw, method="pbkdf2:sha256"))
    except Exception:
        return jsonify(error="An account with this email already exists."), 409
    session["uid"], session["name"] = uid, name or email.split("@")[0].title()
    session.permanent = bool(d.get("remember"))
    return jsonify(ok=True), 201


@app.post("/api/login")
def login():
    d = request.get_json(force=True)
    u = db.get_user((d.get("email") or "").strip().lower())
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        return jsonify(error="Wrong email or password."), 401
    session["uid"], session["name"] = u["id"], u["name"]
    session.permanent = bool(d.get("remember"))
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    return jsonify(id=session["uid"], name=session["name"])


@app.get("/api/check")
@login_required
def check():
    loc = (request.args.get("location") or "").strip()
    start, end = request.args.get("start", ""), request.args.get("end", "")
    c = db.find_conflict(loc, start, end) if loc and start and end else None
    if c:
        return jsonify(available=False, message=booked_message(loc, c))
    return jsonify(available=True, message="This location is available at that time.")


@app.post("/api/meetings")
@login_required
def create_meeting():
    d = request.get_json(force=True)
    title = (d.get("title") or "Untitled meeting").strip()
    loc = (d.get("location") or "").strip()
    start, end = d.get("start_time") or "", d.get("end_time") or ""
    transcript = (d.get("transcript") or "").strip()
    if not loc or len(start) < 16 or len(end) < 16:
        return jsonify(error="Enter a location, date, start time and end time."), 400
    if end <= start:
        return jsonify(error="End time must be after the start time."), 400
    c = db.find_conflict(loc, start, end)
    if c:
        return jsonify(error=booked_message(loc, c)), 409
    used_ai, result = True, {"summary": "", "decisions": [], "actions": []}
    if transcript:
        try:
            result = analyze_with_ai(transcript)
        except Exception as e:
            print("AI analysis failed, using basic mode:", e)
            used_ai, result = False, analyze_basic(transcript)
    mid = db.add_meeting(session["uid"], title, loc, start, end, result.get("summary", ""),
                         result.get("decisions", []), result.get("actions", []))
    return jsonify(id=mid, used_ai=used_ai), 201


@app.get("/api/meetings")
@login_required
def meetings():
    return jsonify(db.list_meetings())


@app.get("/api/actions")
@login_required
def actions():
    return jsonify(db.list_actions())


@app.patch("/api/actions/<int:aid>")
@login_required
def update_action(aid):
    status = request.get_json(force=True).get("status")
    if status not in ("todo", "doing", "done"):
        return jsonify(error="Invalid status"), 400
    db.set_status(aid, status)
    return jsonify(ok=True)


@app.delete("/api/meetings/<int:mid>")
@login_required
def remove_meeting(mid):
    if not db.delete_meeting(mid, session["uid"]):
        return jsonify(error="Only the organizer can delete this meeting."), 403
    return jsonify(ok=True)


@app.get("/api/meetings/<int:mid>/mom")
@login_required
def mom(mid):
    """Build a Minutes of Meeting (MoM) Word report for one meeting."""
    from docx import Document
    from docx.shared import Pt
    m = next((x for x in db.list_meetings() if x["id"] == mid), None)
    if not m:
        return jsonify(error="Meeting not found."), 404
    acts = [x for x in db.list_actions() if x["meeting_id"] == mid]
    today = date.today().isoformat()
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading("Minutes of Meeting", 0)
    info = doc.add_table(rows=0, cols=2)
    info.style = "Light Grid Accent 1"
    for k, v in [("Meeting", m["title"]), ("Date", m["start_time"][:10]),
                 ("Time", f'{m["start_time"][11:]} to {m["end_time"][11:]}'),
                 ("Location", m["location"]), ("Organizer", m["organizer"] or "")]:
        row = info.add_row().cells
        row[0].text, row[1].text = k, v
    doc.add_heading("Summary", 1)
    doc.add_paragraph(m["summary"] or "No summary recorded.")
    doc.add_heading("Key Decisions", 1)
    if m["decisions"]:
        for d in m["decisions"]:
            doc.add_paragraph(d, style="List Number")
    else:
        doc.add_paragraph("No decisions recorded.")
    doc.add_heading("Action Items", 1)
    if acts:
        t = doc.add_table(rows=1, cols=4)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(["Task", "Responsible", "Deadline", "Status"]):
            t.rows[0].cells[i].text = h
        names = {"todo": "Pending", "doing": "In Progress", "done": "Completed"}
        for x in acts:
            status = "Overdue" if x["due"] and x["due"] < today and x["status"] != "done" else names[x["status"]]
            c = t.add_row().cells
            c[0].text, c[1].text = x["task"], x["owner"] or "Unassigned"
            c[2].text, c[3].text = x["due"] or "Not set", status
    else:
        doc.add_paragraph("No action items recorded.")
    doc.add_paragraph()
    doc.add_paragraph(f"Generated by Meet2Act AI on {today}.").runs[0].italic = True
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", m["title"]).strip("_") or "meeting"
    return send_file(buf, as_attachment=True, download_name=f"MoM_{safe}.docx",
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.post("/api/ask")
@login_required
def ask():
    q = (request.get_json(force=True).get("question") or "").strip()
    if not q:
        return jsonify(error="Type a question first."), 400
    ctx = "\n".join(
        f"- {m['title']} ({m['start_time']}, {m['location']}): {m['summary']} "
        f"Decisions: {'; '.join(m['decisions'])}" for m in db.list_meetings()[:20])
    ctx += "\nActions:\n" + "\n".join(
        f"- {a['task']} | owner {a['owner'] or 'unassigned'} | due {a['due'] or 'none'} | "
        f"{a['status']} | {a['meeting']}" for a in db.list_actions()[:80])
    try:
        answer = ask_claude(f"Today is {date.today().isoformat()}. Using only this meeting data, "
                            f"answer briefly.\n\n{ctx}\n\nQuestion: {q}", 800)
    except Exception as e:
        print("Ask AI failed:", e)
        return jsonify(error="AI isn't connected yet. Set ANTHROPIC_API_KEY and restart the app."), 503
    return jsonify(answer=answer)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)