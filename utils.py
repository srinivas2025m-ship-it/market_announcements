"""Small helpers shared across the navigation pages."""

from datetime import date
from urllib.parse import quote_plus


def days_label(planned_date_str):
    if not planned_date_str:
        return "—"
    d = date.fromisoformat(planned_date_str)
    delta = (d - date.today()).days
    if delta < 0:
        return f"⚠️ {abs(delta)}d overdue"
    if delta == 0:
        return "🟠 due today"
    if delta <= 3:
        return f"🟡 due in {delta}d"
    return f"{d.isoformat()}"


def search_recommendation_url(title):
    year = date.today().year
    query = f"{title} {year} report"
    return f"https://www.google.com/search?q={quote_plus(query)}"


def article_picker_label(a):
    return f"[{a['category']}] {a['title']} (#{a['id']})"
