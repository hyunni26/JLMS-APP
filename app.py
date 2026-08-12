import os
import sqlite3
import tempfile
from datetime import timedelta, datetime
from functools import wraps

from flask import Flask, render_template, redirect, url_for, request, session, flash

from s3_db import list_db_backups, download_db_backup

app = Flask(__name__)
app.secret_key = os.environ.get("VIEWER_SECRET_KEY", "jmo-lms-viewer-dev-key-change-me")

VIEW_PASSWORD = os.environ.get("VIEWER_PASSWORD", "1234")
DB_PATH = os.path.join(tempfile.gettempdir(), "jmo_lms_viewer.db")


# ---------------- DB 접근 ----------------
def get_db():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_last_synced():
    if not os.path.exists(DB_PATH):
        return None
    mtime = os.path.getmtime(DB_PATH)
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 로그인 보호 ----------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == VIEW_PASSWORD:
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=30)
            session["logged_in"] = True
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        flash("비밀번호가 올바르지 않습니다.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- DB 동기화 ----------------
@app.route("/sync", methods=["POST"])
@login_required
def sync_db():
    ok, entries = list_db_backups()
    if not ok:
        flash(f"서버 목록 조회 실패: {entries}")
        return redirect(url_for("dashboard"))
    if not entries:
        flash("서버에 저장된 백업이 없습니다.")
        return redirect(url_for("dashboard"))

    latest = entries[0]
    ok2, msg = download_db_backup(latest["key"], DB_PATH)
    if ok2:
        flash("최신 데이터로 업데이트했습니다.")
    else:
        flash(f"다운로드 실패: {msg}")
    return redirect(url_for("dashboard"))


# ---------------- 대시보드 ----------------
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", last_synced=db_last_synced())


# ---------------- 1. 공지사항 ----------------
@app.route("/notices")
@login_required
def notices():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))
    try:
        rows = conn.execute("SELECT * FROM notices ORDER BY created_at DESC, id DESC").fetchall()
        work_notes = conn.execute(
            "SELECT * FROM work_notes ORDER BY created_at DESC, id DESC LIMIT 50"
        ).fetchall()
    except sqlite3.Error as e:
        conn.close()
        flash(f"공지사항 조회 실패: {e}")
        return redirect(url_for("dashboard"))
    conn.close()
    return render_template("notices.html", notices=rows, work_notes=work_notes)


# ---------------- 2. 거래처 원장 ----------------
@app.route("/ledger")
@login_required
def ledger_company_list():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))
    rows = conn.execute(
        """
        SELECT c.*,
               COALESCE((SELECT SUM(total_amount) FROM statements WHERE company_id = c.id), 0)
               - COALESCE((SELECT SUM(amount) FROM payments WHERE company_id = c.id), 0) as balance
        FROM companies c ORDER BY c.name
        """
    ).fetchall()
    conn.close()
    return render_template("ledger_list.html", companies=rows)


@app.route("/ledger/<int:company_id>")
@login_required
def ledger_detail(company_id):
    conn = get_db()
    if conn is None:
        return redirect(url_for("dashboard"))
    company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    statements = conn.execute(
        "SELECT statement_date as date, total_amount as amount, 'statement' as kind, id "
        "FROM statements WHERE company_id=?",
        (company_id,),
    ).fetchall()
    payments = conn.execute(
        "SELECT payment_date as date, amount, 'payment' as kind, id FROM payments WHERE company_id=?",
        (company_id,),
    ).fetchall()
    entries = sorted(
        [dict(r) for r in statements] + [dict(r) for r in payments],
        key=lambda e: (e["date"] or "", 0 if e["kind"] == "statement" else 1, e["id"]),
    )
    balance = 0
    for e in entries:
        if e["kind"] == "statement":
            balance += e["amount"]
        else:
            balance -= e["amount"]
        e["balance"] = balance
    conn.close()
    return render_template("ledger_detail.html", company=company, entries=entries, final_balance=balance)


# ---------------- 3. 거래처 관리 (보기 전용) ----------------
@app.route("/companies")
@login_required
def companies():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))
    keyword = request.args.get("q", "").strip()
    if keyword:
        rows = conn.execute(
            "SELECT * FROM companies WHERE name LIKE ? ORDER BY name", (f"%{keyword}%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM companies ORDER BY name").fetchall()
    conn.close()
    return render_template("companies.html", companies=rows, keyword=keyword)


# ---------------- 4. 렌즈 관리 (보기 전용) ----------------
@app.route("/lens-types")
@login_required
def lens_types():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))
    types = conn.execute("SELECT * FROM lens_types ORDER BY name").fetchall()
    items_by_type = {}
    for t in types:
        items = conn.execute(
            "SELECT * FROM lens_type_items WHERE lens_type_id=? ORDER BY name", (t["id"],)
        ).fetchall()
        items_by_type[t["id"]] = items
    conn.close()
    return render_template("lens_types.html", lens_types=types, items_by_type=items_by_type)


# ---------------- 5. RX(야간) 작업 현황 ----------------
@app.route("/night-work")
@login_required
def night_work():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    date_filter = request.args.get("date", "").strip()
    query = """
        SELECT ni.*, e.work_date, e.company_id, c.name as company_name,
               pt.name as process_type_name
        FROM night_work_items ni
        JOIN night_work_entries e ON ni.entry_id = e.id
        LEFT JOIN companies c ON e.company_id = c.id
        LEFT JOIN process_types pt ON ni.process_type_id = pt.id
    """
    params = ()
    if date_filter:
        query += " WHERE e.work_date = ?"
        params = (date_filter,)
    query += " ORDER BY e.work_date DESC, c.name"
    rows = conn.execute(query, params).fetchall()

    dates = conn.execute(
        "SELECT DISTINCT work_date FROM night_work_entries ORDER BY work_date DESC LIMIT 60"
    ).fetchall()
    conn.close()
    return render_template("night_work.html", rows=rows, dates=dates, selected_date=date_filter)


# ---------------- 6. 생산 작업 현황 (하드실/코팅실/완제품포장) ----------------
def _production_summary(conn, table, extra_cols=""):
    return conn.execute(
        f"""
        SELECT work_date, COUNT(*) as cnt, SUM(input_qty) as input_sum, SUM(output_qty) as output_sum
        {extra_cols}
        FROM {table} GROUP BY work_date ORDER BY work_date DESC LIMIT 60
        """
    ).fetchall()


@app.route("/production")
@login_required
def production():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    room = request.args.get("room", "hardroom")
    date_filter = request.args.get("date", "").strip()

    if room == "coatingroom":
        table, has_output = "coatingroom_logs", True
    elif room == "packing":
        table, has_output = "packing_logs", False
    else:
        room, table, has_output = "hardroom", "hardroom_logs", True

    if has_output:
        summary = conn.execute(
            f"SELECT work_date, COUNT(*) as cnt, SUM(input_qty) as input_sum, SUM(output_qty) as output_sum "
            f"FROM {table} GROUP BY work_date ORDER BY work_date DESC LIMIT 60"
        ).fetchall()
    else:
        summary = conn.execute(
            f"SELECT work_date, COUNT(*) as cnt, SUM(input_qty) as input_sum, SUM(defect_qty) as defect_sum "
            f"FROM {table} GROUP BY work_date ORDER BY work_date DESC LIMIT 60"
        ).fetchall()

    detail = None
    if date_filter:
        if table == "hardroom_logs":
            detail = conn.execute(
                """SELECT hl.*, c.name as company_name, lt.name as lens_type_name, lti.name as lens_item_name,
                          ov.name as oven_name
                   FROM hardroom_logs hl
                   LEFT JOIN companies c ON hl.company_id=c.id
                   LEFT JOIN lens_types lt ON hl.lens_type_id=lt.id
                   LEFT JOIN lens_type_items lti ON hl.lens_type_item_id=lti.id
                   LEFT JOIN ovens ov ON hl.oven_id=ov.id
                   WHERE hl.work_date=? ORDER BY hl.id""",
                (date_filter,),
            ).fetchall()
        elif table == "coatingroom_logs":
            detail = conn.execute(
                """SELECT cl.*, c.name as company_name, lt.name as lens_type_name, lti.name as lens_item_name,
                          cm.name as machine_name
                   FROM coatingroom_logs cl
                   LEFT JOIN companies c ON cl.company_id=c.id
                   LEFT JOIN lens_types lt ON cl.lens_type_id=lt.id
                   LEFT JOIN lens_type_items lti ON cl.lens_type_item_id=lti.id
                   LEFT JOIN coating_machines cm ON cl.machine_id=cm.id
                   WHERE cl.work_date=? ORDER BY cl.id""",
                (date_filter,),
            ).fetchall()
        else:
            detail = conn.execute(
                """SELECT pl.*, lt.name as lens_type_name, lti.name as lens_item_name
                   FROM packing_logs pl
                   LEFT JOIN lens_types lt ON pl.lens_type_id=lt.id
                   LEFT JOIN lens_type_items lti ON pl.lens_type_item_id=lti.id
                   WHERE pl.work_date=? ORDER BY pl.id""",
                (date_filter,),
            ).fetchall()

    conn.close()
    return render_template(
        "production.html", room=room, summary=summary, detail=detail,
        selected_date=date_filter, has_output=has_output,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
