{% extends "base.html" %}
{% block content %}
<div class="breadcrumb"><a href="{{ url_for('dashboard') }}">← 목록</a></div>
<h1>거래처 미수금 현황</h1>

<div class="card">
  <form method="get" class="filter-row">
    <input type="date" name="start" value="{{ start }}" onchange="this.form.submit()">
    <span style="color:var(--text-light);">~</span>
    <input type="date" name="end" value="{{ end }}" onchange="this.form.submit()">
  </form>
</div>

<div class="card table-scroll">
  <table>
    <tr>
      <th>거래처명</th>
      <th class="num">전미수금</th>
      <th class="num">매출</th>
      <th class="num">반품</th>
      <th class="num">입금</th>
      <th class="num">할인</th>
      <th class="num">이월미수금</th>
      <th class="num">순매출액</th>
    </tr>
    {% for r in rows %}
    <tr onclick="location.href='{{ url_for('ledger_detail', company_id=r.id, start=start, end=end) }}'" style="cursor:pointer;">
      <td>{{ r.name }}</td>
      <td class="num">{{ "{:,.0f}".format(r.prior) }}</td>
      <td class="num">{{ "{:,.0f}".format(r.sales) }}</td>
      <td class="num">{{ "{:,.0f}".format(r.returns) }}</td>
      <td class="num">{{ "{:,.0f}".format(r.payment) }}</td>
      <td class="num">{{ "{:,.0f}".format(r.discount) }}</td>
      <td class="num" style="font-weight:600;">{{ "{:,.0f}".format(r.carry) }}</td>
      <td class="num">{{ "{:,.0f}".format(r.net_sales) }}</td>
    </tr>
    {% else %}
    <tr><td colspan="8" class="empty">해당 기간에 표시할 거래처가 없습니다.</td></tr>
    {% endfor %}
    {% if rows %}
    <tr style="background:var(--gold); font-weight:700;">
      <td>합계</td>
      <td class="num">{{ "{:,.0f}".format(totals.prior) }}</td>
      <td class="num">{{ "{:,.0f}".format(totals.sales) }}</td>
      <td class="num">{{ "{:,.0f}".format(totals.returns) }}</td>
      <td class="num">{{ "{:,.0f}".format(totals.payment) }}</td>
      <td class="num">{{ "{:,.0f}".format(totals.discount) }}</td>
      <td class="num">{{ "{:,.0f}".format(totals.carry) }}</td>
      <td class="num">{{ "{:,.0f}".format(totals.net_sales) }}</td>
    </tr>
    {% endif %}
  </table>
</div>
{% endblock %}
