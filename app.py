import os
import sqlite3
import tempfile
from datetime import timedelta, datetime, timezone
from functools import wraps

from flask import Flask, render_template, redirect, url_for, request, session, flash

from s3_db import list_db_backups, download_db_backup

app = Flask(__name__)
app.secret_key = os.environ.get("VIEWER_SECRET_KEY", "jmo-lms-viewer-dev-key-change-me")

VIEW_PASSWORD = os.environ.get("VIEWER_PASSWORD", "1234")
DB_PATH = os.path.join(tempfile.gettempdir(), "jmo_lms_viewer.db")

# Render 서버는 UTC로 동작하므로, 화면 표시/날짜 기본값은 한국시간(UTC+9) 기준으로 맞춘다.
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


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
    synced = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(KST)
    return synced.strftime("%Y-%m-%d %H:%M:%S")


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
    rows = conn.execute("SELECT * FROM notices ORDER BY created_at DESC, id DESC").fetchall()
    work_notes = conn.execute(
        "SELECT * FROM work_notes ORDER BY created_at DESC, id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return render_template("notices.html", notices=rows, work_notes=work_notes)


# ---------------- 2. 거래처 원장 ----------------
def _parse_discount(raw):
    """조회용 할인율(%) 파싱. 잘못된 값이거나 없으면 0으로 처리."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return min(max(val, 0.0), 100.0)


@app.route("/ledger")
@login_required
def ledger_company_list():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    company_id = request.args.get("company_id", "").strip()
    if company_id:
        conn.close()
        kwargs = {"company_id": company_id}
        discount_raw = request.args.get("discount", "").strip()
        if discount_raw:
            kwargs["discount"] = discount_raw
        return redirect(url_for("ledger_detail", **kwargs))

    companies = conn.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    conn.close()
    return render_template("ledger_list.html", companies=companies)


@app.route("/ledger/<int:company_id>")
@login_required
def ledger_detail(company_id):
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    today = now_kst().strftime("%Y-%m-%d")
    default_start = (now_kst() - timedelta(days=30)).strftime("%Y-%m-%d")
    start = request.args.get("start", "").strip() or default_start
    end = request.args.get("end", "").strip() or today
    discount = _parse_discount(request.args.get("discount", ""))

    company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if company is None:
        conn.close()
        flash("거래처를 찾을 수 없습니다.")
        return redirect(url_for("ledger_company_list"))

    # 이월잔액: 조회 시작일 이전까지의 누적 잔액 (할인율은 거래(statement) 금액에만 적용, 조회용 계산이며 DB에는 저장하지 않음)
    prior_statement_raw = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) as s FROM statements WHERE company_id=? AND statement_date < ?",
        (company_id, start),
    ).fetchone()["s"]
    prior_payment = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as s FROM payments WHERE company_id=? AND payment_date < ?",
        (company_id, start),
    ).fetchone()["s"]
    opening_balance = prior_statement_raw * (1 - discount / 100) - prior_payment

    statements = conn.execute(
        "SELECT statement_date as date, total_amount as amount, 'statement' as kind, id "
        "FROM statements WHERE company_id=? AND statement_date BETWEEN ? AND ?",
        (company_id, start, end),
    ).fetchall()
    payments = conn.execute(
        "SELECT payment_date as date, amount, 'payment' as kind, id FROM payments "
        "WHERE company_id=? AND payment_date BETWEEN ? AND ?",
        (company_id, start, end),
    ).fetchall()
    entries = sorted(
        [dict(r) for r in statements] + [dict(r) for r in payments],
        key=lambda e: (e["date"] or "", 0 if e["kind"] == "statement" else 1, e["id"]),
    )
    balance = opening_balance
    for e in entries:
        if e["kind"] == "statement":
            e["calc_amount"] = e["amount"] * (1 - discount / 100)
            balance += e["calc_amount"]
        else:
            e["calc_amount"] = e["amount"]
            balance -= e["calc_amount"]
        e["balance"] = balance
    conn.close()
    return render_template(
        "ledger_detail.html",
        company=company, entries=entries, final_balance=balance,
        opening_balance=opening_balance, start=start, end=end, discount=discount,
    )


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

    latest = conn.execute("SELECT MAX(work_date) as d FROM night_work_entries").fetchone()["d"]
    default_date = latest or now_kst().strftime("%Y-%m-%d")
    start = request.args.get("start", "").strip() or default_date
    end = request.args.get("end", "").strip() or default_date
    view = request.args.get("view", "company")
    if view not in ("company", "total"):
        view = "company"

    rows = []
    if start and end:
        query = """
            SELECT ni.*, e.work_date, e.company_id, c.name as company_name,
                   pt.name as process_type_name
            FROM night_work_items ni
            JOIN night_work_entries e ON ni.entry_id = e.id
            LEFT JOIN companies c ON e.company_id = c.id
            LEFT JOIN process_types pt ON ni.process_type_id = pt.id
            WHERE e.work_date BETWEEN ? AND ?
            ORDER BY c.name, ni.id
        """
        rows = conn.execute(query, (start, end)).fetchall()
    conn.close()

    processed = []
    for r in rows:
        d = dict(r)
        d["total_qty"] = (d["work_qty"] or 0) + (d["cut_qty"] or 0) + (d["rework_qty"] or 0)
        processed.append(d)

    if view == "total":
        # 거래처 구분 없이 처리종류별로만 합산 (작업/컷팅/재작업 각각 별도 합계)
        totals = {}
        order = []
        for d in processed:
            key = d["process_type_name"] or "-"
            if key not in totals:
                totals[key] = {
                    "process_type_name": key,
                    "work_qty": 0, "cut_qty": 0, "rework_qty": 0, "total_qty": 0,
                }
                order.append(key)
            totals[key]["work_qty"] += d["work_qty"] or 0
            totals[key]["cut_qty"] += d["cut_qty"] or 0
            totals[key]["rework_qty"] += d["rework_qty"] or 0
            totals[key]["total_qty"] += d["total_qty"]
        result_rows = [totals[k] for k in order]
        return render_template("night_work.html", rows=result_rows, start=start, end=end, view=view)

    # 거래처별: 같은 거래처가 이어지는 구간만큼 rowspan 계산
    i = 0
    while i < len(processed):
        j = i
        while j < len(processed) and processed[j]["company_name"] == processed[i]["company_name"]:
            j += 1
        processed[i]["company_rowspan"] = j - i
        for k in range(i + 1, j):
            processed[k]["company_rowspan"] = 0
        i = j

    return render_template("night_work.html", rows=processed, start=start, end=end, view=view)


# ---------------- 6. 생산 작업 현황 (하드실/코팅실/완제품포장) ----------------
@app.route("/production")
@login_required
def production():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    room = request.args.get("room", "hardroom")
    date_filter = request.args.get("date", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()

    if room == "coatingroom":
        table, has_output = "coatingroom_logs", True
    elif room == "packing":
        table, has_output = "packing_logs", False
    else:
        room, table, has_output = "hardroom", "hardroom_logs", True

    where_clause = ""
    params = ()
    if start and end:
        where_clause = "WHERE work_date BETWEEN ? AND ?"
        params = (start, end)
    elif start:
        where_clause = "WHERE work_date >= ?"
        params = (start,)
    elif end:
        where_clause = "WHERE work_date <= ?"
        params = (end,)

    if has_output:
        summary = conn.execute(
            f"SELECT work_date, COUNT(*) as cnt, SUM(input_qty) as input_sum, SUM(output_qty) as output_sum "
            f"FROM {table} {where_clause} GROUP BY work_date ORDER BY work_date DESC LIMIT 60",
            params,
        ).fetchall()
    else:
        summary = conn.execute(
            f"SELECT work_date, COUNT(*) as cnt, SUM(input_qty) as input_sum, SUM(defect_qty) as defect_sum "
            f"FROM {table} {where_clause} GROUP BY work_date ORDER BY work_date DESC LIMIT 60",
            params,
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
        selected_date=date_filter, has_output=has_output, start=start, end=end,
    )


# ---------------- 6-1. 생산 이력 (기간 + 품목 집계) ----------------
@app.route("/production/history")
@login_required
def production_history():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    room = request.args.get("room", "hardroom")
    if room not in ("hardroom", "coatingroom", "packing"):
        room = "hardroom"
    table = {"hardroom": "hardroom_logs", "coatingroom": "coatingroom_logs", "packing": "packing_logs"}[room]
    has_output = room in ("hardroom", "coatingroom")

    today = now_kst().strftime("%Y-%m-%d")
    default_start = (now_kst() - timedelta(days=30)).strftime("%Y-%m-%d")
    start = request.args.get("start", "").strip() or default_start
    end = request.args.get("end", "").strip() or today

    lens_types = conn.execute("SELECT id, name FROM lens_types ORDER BY name").fetchall()
    lens_type_id = request.args.get("lens_type_id", "").strip()
    lens_type_name = "전체"
    if lens_type_id:
        lt = conn.execute("SELECT name FROM lens_types WHERE id=?", (lens_type_id,)).fetchone()
        lens_type_name = lt["name"] if lt else "전체"

    where = "work_date BETWEEN ? AND ?"
    params = [start, end]
    if lens_type_id:
        where += " AND lens_type_id=?"
        params.append(lens_type_id)

    if has_output:
        total = conn.execute(
            f"""SELECT COUNT(*) as cnt, COALESCE(SUM(input_qty),0) as input_sum,
                       COALESCE(SUM(output_qty),0) as output_sum, COALESCE(SUM(defect_qty),0) as defect_sum,
                       COALESCE(SUM(discard_qty),0) as discard_sum
                FROM {table} WHERE {where}""",
            params,
        ).fetchone()
        by_lens_type = conn.execute(
            f"""SELECT lt.id as lens_type_id, lt.name, COUNT(*) as cnt,
                       COALESCE(SUM(t.input_qty),0) as input_sum, COALESCE(SUM(t.output_qty),0) as output_sum,
                       COALESCE(SUM(t.defect_qty),0) as defect_sum
                FROM {table} t
                LEFT JOIN lens_types lt ON t.lens_type_id = lt.id
                WHERE {where}
                GROUP BY t.lens_type_id ORDER BY input_sum DESC""",
            params,
        ).fetchall()
    else:
        total = conn.execute(
            f"""SELECT COUNT(*) as cnt, COALESCE(SUM(input_qty),0) as input_sum,
                       COALESCE(SUM(defect_qty),0) as defect_sum
                FROM {table} WHERE {where}""",
            params,
        ).fetchone()
        by_lens_type = conn.execute(
            f"""SELECT lt.id as lens_type_id, lt.name, COUNT(*) as cnt,
                       COALESCE(SUM(t.input_qty),0) as input_sum, COALESCE(SUM(t.defect_qty),0) as defect_sum
                FROM {table} t
                LEFT JOIN lens_types lt ON t.lens_type_id = lt.id
                WHERE {where}
                GROUP BY t.lens_type_id ORDER BY input_sum DESC""",
            params,
        ).fetchall()
    conn.close()

    total = dict(total) if total else {}
    input_sum = total.get("input_sum", 0) or 0
    if has_output:
        output_sum = total.get("output_sum", 0) or 0
        rate = (output_sum / input_sum * 100) if input_sum else 0
    else:
        defect_sum = total.get("defect_sum", 0) or 0
        rate = ((input_sum - defect_sum) / input_sum * 100) if input_sum else 0

    return render_template(
        "production_history.html",
        room=room, has_output=has_output, start=start, end=end,
        lens_types=lens_types, lens_type_id=lens_type_id, lens_type_name=lens_type_name,
        total=total, rate=rate, by_lens_type=by_lens_type,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
