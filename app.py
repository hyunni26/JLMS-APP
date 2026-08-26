import os
import sqlite3
import tempfile
from datetime import timedelta, datetime, timezone
from functools import wraps

from flask import Flask, render_template, redirect, url_for, request, session, flash

from s3_db import DB_NAMES, sync_all_dbs

app = Flask(__name__)
app.secret_key = os.environ.get("VIEWER_SECRET_KEY", "jmo-lms-viewer-dev-key-change-me")

VIEW_PASSWORD = os.environ.get("VIEWER_PASSWORD", "1234")

# 기존 통합 lens_manager.db가 역할별 6개 파일로 분리됨. 파일명 = ATTACH 스키마 alias.
# main.db          -> orders/statements/payments/notices 등 (SQLite 기본 스키마 "main"과 이름이 같아 자연스럽게 매핑됨)
# company_master.db -> companies/lens_types/process_types/ovens/coating_machines 등
# hardroom.db / coatingroom.db / rx.db / packing.db -> 각 작업실 로그
DB_PATHS = {name: os.path.join(tempfile.gettempdir(), f"jmo_{name}.db") for name in DB_NAMES}

# Render 서버는 UTC로 동작하므로, 화면 표시/날짜 기본값은 한국시간(UTC+9) 기준으로 맞춘다.
KST = timezone(timedelta(hours=9))

DB_LABELS = {
    "main": "기본(거래/공지)", "company_master": "거래처/렌즈",
    "hardroom": "하드실", "coatingroom": "코팅실", "rx": "RX작업", "packing": "완제품포장",
}


def now_kst():
    return datetime.now(KST)


# ---------------- DB 접근 ----------------
def get_db():
    """6개 DB 파일이 전부 있어야 연결한다. main.db를 기본 커넥션으로 열고 나머지 5개를 ATTACH한다."""
    if not all(os.path.exists(DB_PATHS[n]) for n in DB_NAMES):
        return None
    conn = sqlite3.connect(DB_PATHS["main"])
    conn.row_factory = sqlite3.Row
    for name in DB_NAMES:
        if name == "main":
            continue
        conn.execute("ATTACH DATABASE ? AS " + name, (DB_PATHS[name],))
    return conn


def db_sync_status():
    """DB 파일별 마지막 동기화 시각(KST). 파일이 없으면 None."""
    status = {}
    for name in DB_NAMES:
        p = DB_PATHS[name]
        if os.path.exists(p):
            mtime = os.path.getmtime(p)
            synced = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone(KST)
            status[name] = synced.strftime("%Y-%m-%d %H:%M:%S")
        else:
            status[name] = None
    return status


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
    all_ok, results = sync_all_dbs(DB_PATHS)
    failed = [DB_LABELS[name] for name, (ok, _) in results.items() if not ok]
    if all_ok:
        flash("6개 DB 모두 최신 데이터로 업데이트했습니다.")
    elif failed:
        flash(f"일부 실패: {', '.join(failed)} 동기화에 실패했습니다. (나머지는 갱신됨)")
    return redirect(url_for("dashboard"))


# ---------------- 대시보드 ----------------
@app.route("/")
@login_required
def dashboard():
    status = db_sync_status()
    sync_rows = [{"name": n, "label": DB_LABELS[n], "synced": status[n]} for n in DB_NAMES]
    return render_template("dashboard.html", sync_rows=sync_rows)


# ---------------- 1. 공지사항 ----------------
@app.route("/notices")
@login_required
def notices():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))
    rows = conn.execute("SELECT * FROM main.notices ORDER BY created_at DESC, id DESC").fetchall()
    conn.close()
    return render_template("notices.html", notices=rows)


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

    companies = conn.execute("SELECT id, name FROM company_master.companies ORDER BY name").fetchall()
    conn.close()
    return render_template("ledger_list.html", companies=companies)


@app.route("/ledger/<int:company_id>")
@login_required
def ledger_detail(company_id):
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    today_kst = now_kst()
    today = today_kst.strftime("%Y-%m-%d")
    default_start = today_kst.replace(day=1).strftime("%Y-%m-%d")
    start = request.args.get("start", "").strip() or default_start
    end = request.args.get("end", "").strip() or today
    discount = _parse_discount(request.args.get("discount", ""))

    company = conn.execute("SELECT * FROM company_master.companies WHERE id=?", (company_id,)).fetchone()
    if company is None:
        conn.close()
        flash("거래처를 찾을 수 없습니다.")
        return redirect(url_for("ledger_company_list"))

    # 이월잔액: 조회 시작일 이전까지의 누적 잔액 (할인율은 거래(statement) 금액에만 적용, 조회용 계산이며 DB에는 저장하지 않음)
    prior_statement_raw = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) as s FROM main.statements WHERE company_id=? AND statement_date < ?",
        (company_id, start),
    ).fetchone()["s"]
    prior_payment = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as s FROM main.payments WHERE company_id=? AND payment_date < ?",
        (company_id, start),
    ).fetchone()["s"]
    opening_balance = prior_statement_raw * (1 - discount / 100) - prior_payment

    statements = conn.execute(
        "SELECT statement_date as date, total_amount as amount, 'statement' as kind, id "
        "FROM main.statements WHERE company_id=? AND statement_date BETWEEN ? AND ?",
        (company_id, start, end),
    ).fetchall()
    payments = conn.execute(
        "SELECT payment_date as date, amount, 'payment' as kind, id FROM main.payments "
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


# ---------------- 2-1. 거래처 전체 미수금 현황 ----------------
@app.route("/receivables")
@login_required
def receivables():
    conn = get_db()
    if conn is None:
        flash("먼저 데이터를 불러와주세요.")
        return redirect(url_for("dashboard"))

    today_kst = now_kst()
    today = today_kst.strftime("%Y-%m-%d")
    default_start = today_kst.replace(day=1).strftime("%Y-%m-%d")
    start = request.args.get("start", "").strip() or default_start
    end = request.args.get("end", "").strip() or today

    companies_rows = conn.execute(
        "SELECT id, name, discount_rate FROM company_master.companies ORDER BY name"
    ).fetchall()

    # 전미수금 구성요소: 조회 시작일 이전의 명세서 합계 / 입금 합계 (거래처별)
    prior_statements = {
        r["company_id"]: r["s"] for r in conn.execute(
            "SELECT company_id, COALESCE(SUM(total_amount),0) as s FROM main.statements "
            "WHERE statement_date < ? GROUP BY company_id",
            (start,),
        ).fetchall()
    }
    prior_payments = {
        r["company_id"]: r["s"] for r in conn.execute(
            "SELECT company_id, COALESCE(SUM(amount),0) as s FROM main.payments "
            "WHERE payment_date < ? GROUP BY company_id",
            (start,),
        ).fetchall()
    }

    # 기간 내 매출/반품: statement_items.amount 부호로 구분 (양수=매출, 음수=반품)
    period_sales_returns = {
        r["company_id"]: (r["sales"], r["returns"]) for r in conn.execute(
            """SELECT s.company_id,
                      COALESCE(SUM(CASE WHEN si.amount > 0 THEN si.amount ELSE 0 END),0) as sales,
                      COALESCE(SUM(CASE WHEN si.amount < 0 THEN -si.amount ELSE 0 END),0) as returns
               FROM main.statement_items si
               JOIN main.statements s ON si.statement_id = s.id
               WHERE s.statement_date BETWEEN ? AND ?
               GROUP BY s.company_id""",
            (start, end),
        ).fetchall()
    }

    # 기간 내 입금
    period_payments = {
        r["company_id"]: r["s"] for r in conn.execute(
            "SELECT company_id, COALESCE(SUM(amount),0) as s FROM main.payments "
            "WHERE payment_date BETWEEN ? AND ? GROUP BY company_id",
            (start, end),
        ).fetchall()
    }
    conn.close()

    rows = []
    totals = {"prior": 0, "sales": 0, "returns": 0, "payment": 0, "discount": 0, "carry": 0, "net_sales": 0}
    for c in companies_rows:
        cid = c["id"]
        rate = c["discount_rate"] or 0
        prior_payment_sum = prior_payments.get(cid, 0)
        opening_balance = prior_statements.get(cid, 0) - prior_payment_sum - prior_payment_sum * rate / 100

        sales, returns = period_sales_returns.get(cid, (0, 0))
        payment = period_payments.get(cid, 0)
        discount = payment * rate / 100
        net_sales = sales - returns
        carry = opening_balance + sales - returns - payment - discount

        # 전미수금/매출/반품/입금 전부 0인 거래처는 표에서 제외 (기간 내 무거래 + 이월잔액 없음)
        if opening_balance == 0 and sales == 0 and returns == 0 and payment == 0:
            continue

        rows.append({
            "id": cid, "name": c["name"], "prior": opening_balance,
            "sales": sales, "returns": returns, "payment": payment,
            "discount": discount, "carry": carry, "net_sales": net_sales,
        })
        totals["prior"] += opening_balance
        totals["sales"] += sales
        totals["returns"] += returns
        totals["payment"] += payment
        totals["discount"] += discount
        totals["carry"] += carry
        totals["net_sales"] += net_sales

    return render_template(
        "receivables.html", rows=rows, totals=totals, start=start, end=end,
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
            "SELECT * FROM company_master.companies WHERE name LIKE ? ORDER BY name", (f"%{keyword}%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM company_master.companies ORDER BY name").fetchall()
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
    types = conn.execute("SELECT * FROM company_master.lens_types ORDER BY name").fetchall()
    items_by_type = {}
    for t in types:
        items = conn.execute(
            "SELECT * FROM company_master.lens_type_items WHERE lens_type_id=? ORDER BY name", (t["id"],)
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

    latest = conn.execute("SELECT MAX(work_date) as d FROM rx.night_work_entries").fetchone()["d"]
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
            FROM rx.night_work_items ni
            JOIN rx.night_work_entries e ON ni.entry_id = e.id
            LEFT JOIN company_master.companies c ON e.company_id = c.id
            LEFT JOIN company_master.process_types pt ON ni.process_type_id = pt.id
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
        grand_total = {
            "process_type_name": "합계",
            "work_qty": sum(t["work_qty"] for t in result_rows),
            "cut_qty": sum(t["cut_qty"] for t in result_rows),
            "rework_qty": sum(t["rework_qty"] for t in result_rows),
            "total_qty": sum(t["total_qty"] for t in result_rows),
        }
        return render_template(
            "night_work.html", rows=result_rows, grand_total=grand_total,
            start=start, end=end, view=view,
        )

    # 거래처별: 같은 거래처가 이어지는 구간만큼 rowspan 계산 + 거래처 총합계
    i = 0
    while i < len(processed):
        j = i
        while j < len(processed) and processed[j]["company_name"] == processed[i]["company_name"]:
            j += 1
        processed[i]["company_rowspan"] = j - i
        processed[i]["company_total"] = sum(processed[k]["total_qty"] for k in range(i, j))
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
        table, has_output = "coatingroom.coatingroom_logs", True
    elif room == "packing":
        table, has_output = "packing.packing_logs", False
    else:
        room, table, has_output = "hardroom", "hardroom.hardroom_logs", True

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
        if room == "hardroom":
            detail = conn.execute(
                """SELECT hl.*, c.name as company_name, lt.name as lens_type_name, lti.name as lens_item_name,
                          ov.name as oven_name
                   FROM hardroom.hardroom_logs hl
                   LEFT JOIN company_master.companies c ON hl.company_id=c.id
                   LEFT JOIN company_master.lens_types lt ON hl.lens_type_id=lt.id
                   LEFT JOIN company_master.lens_type_items lti ON hl.lens_type_item_id=lti.id
                   LEFT JOIN company_master.ovens ov ON hl.oven_id=ov.id
                   WHERE hl.work_date=? ORDER BY hl.id""",
                (date_filter,),
            ).fetchall()
        elif room == "coatingroom":
            detail = conn.execute(
                """SELECT cl.*, c.name as company_name, lt.name as lens_type_name, lti.name as lens_item_name,
                          cm.name as machine_name
                   FROM coatingroom.coatingroom_logs cl
                   LEFT JOIN company_master.companies c ON cl.company_id=c.id
                   LEFT JOIN company_master.lens_types lt ON cl.lens_type_id=lt.id
                   LEFT JOIN company_master.lens_type_items lti ON cl.lens_type_item_id=lti.id
                   LEFT JOIN company_master.coating_machines cm ON cl.machine_id=cm.id
                   WHERE cl.work_date=? ORDER BY cl.id""",
                (date_filter,),
            ).fetchall()
        else:
            detail = conn.execute(
                """SELECT pl.*, lt.name as lens_type_name, lti.name as lens_item_name
                   FROM packing.packing_logs pl
                   LEFT JOIN company_master.lens_types lt ON pl.lens_type_id=lt.id
                   LEFT JOIN company_master.lens_type_items lti ON pl.lens_type_item_id=lti.id
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
    table = {
        "hardroom": "hardroom.hardroom_logs",
        "coatingroom": "coatingroom.coatingroom_logs",
        "packing": "packing.packing_logs",
    }[room]
    has_output = room in ("hardroom", "coatingroom")

    today = now_kst().strftime("%Y-%m-%d")
    default_start = (now_kst() - timedelta(days=30)).strftime("%Y-%m-%d")
    start = request.args.get("start", "").strip() or default_start
    end = request.args.get("end", "").strip() or today

    lens_types = conn.execute("SELECT id, name FROM company_master.lens_types ORDER BY name").fetchall()
    lens_type_id = request.args.get("lens_type_id", "").strip()
    lens_type_name = "전체"
    if lens_type_id:
        lt = conn.execute("SELECT name FROM company_master.lens_types WHERE id=?", (lens_type_id,)).fetchone()
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
                LEFT JOIN company_master.lens_types lt ON t.lens_type_id = lt.id
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
                LEFT JOIN company_master.lens_types lt ON t.lens_type_id = lt.id
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
