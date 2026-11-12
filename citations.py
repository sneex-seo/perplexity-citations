# -*- coding: utf-8 -*-
"""Що цитує Perplexity: проганяє промпти через Perplexity Agent API і показує, які сайти AI бере за джерела.

Для кожного промпту зберігає відповідь, усі знайдені джерела і ті, на які відповідь справді посилається.
Результат: xlsx і HTML-звіт. Журнал пишеться після кожного промпту, тому перерваний прогін можна продовжити.

    python citations.py prompts.example.txt
    python citations.py prompts.txt --domain example.com.ua
    python citations.py prompts.xlsx --effort low --country "" --limit 5
    python citations.py --report-only          # перебудувати звіти з журналу без запитів до API
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

import targets

HERE = Path(__file__).resolve().parent
API_URL = "https://api.perplexity.ai/v1/agent"
REPO_URL = "https://github.com/sneex-seo/perplexity-citations"

EFFORTS = ("fast", "low", "medium", "high")
DEFAULT_EFFORT = "fast"     # один пошук і коротка відповідь; low, medium і high шукають кілька разів
DEFAULT_COUNTRY = "UA"      # пошук «як з України»; порожній рядок - без прив'язки до країни
# Глибші режими витрачають частину ліміту на міркування: з 1500 токенів відповідь у high обривалася
MAX_OUTPUT_TOKENS = {"fast": 1500, "low": 2500, "medium": 4000, "high": 6000}
PAUSE_BETWEEN = 1.0         # секунд між промптами
MAX_RETRIES = 4
REQUEST_TIMEOUT = (15, 600)

# Посилання в тексті відповіді: [1], [1][3], [web:2] або [1, 3]
MARKER = re.compile(r"\[((?:web:)?\d+(?:\s*,\s*(?:web:)?\d+)*)\]")
HEADER_WORDS = {"prompt", "промпт", "запит", "query", "keyword"}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env(path: Path) -> None:
    """Читає .env, не перезаписуючи змінні, які вже задані в оточенні."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_own(domain: str, own: str) -> bool:
    return bool(own) and (domain == own or domain.endswith("." + own))


def hide_tlds(records: list[dict], tlds: list[str]) -> tuple[list[dict], int]:
    """Прибирає зі звітів джерела з доменних зон tlds. Журнал лишається повним."""
    if not tlds:
        return records, 0
    out, hidden = [], 0
    for rec in records:
        keep = [s for s in rec["sources"] if not any(s["domain"].endswith("." + t) for t in tlds)]
        hidden += len(rec["sources"]) - len(keep)
        out.append({**rec, "sources": keep})
    return out, hidden


# ------------------------------- промпти -----------------------------------
def load_prompts(path: Path) -> list[str]:
    """txt - по промпту на рядок (# - коментар), xlsx і csv - перша колонка."""
    if not path.exists():
        sys.exit(f"[!] Файл із промптами не знайдено: {path}")
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        cells = [row[0] for row in wb.active.iter_rows(values_only=True) if row]
        wb.close()
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            cells = [row[0] for row in csv.reader(f) if row]
    else:
        cells = [line for line in path.read_text(encoding="utf-8-sig").splitlines() if not line.strip().startswith("#")]
    prompts = [str(c).strip() for c in cells if c is not None and str(c).strip()]
    if prompts and prompts[0].lower() in HEADER_WORDS:
        prompts = prompts[1:]
    return list(dict.fromkeys(prompts))


# --------------------------------- API -------------------------------------
def build_body(prompt: str, effort: str, country: str, domain_filter: list[str], recency: str) -> dict:
    tool: dict = {"type": "web_search"}
    if country:
        tool["user_location"] = {"country": country}
    filters = {}
    if domain_filter:
        filters["search_domain_filter"] = domain_filter
    if recency:
        filters["search_recency_filter"] = recency
    if filters:
        tool["filters"] = filters
    # Власних instructions не передаємо: саме системний промпт режиму effort змушує модель посилатися на джерела.
    return {"effort": effort, "input": prompt, "max_output_tokens": MAX_OUTPUT_TOKENS[effort], "tools": [tool]}


def call_api(body: dict, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last = ""
    for attempt in range(1, MAX_RETRIES + 1):
        wait = min(60, 3 * 2 ** attempt)
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last = type(e).__name__
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code != 429 and r.status_code < 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            last = f"HTTP {r.status_code}"
            try:
                wait = int(r.headers.get("Retry-After") or wait)
            except ValueError:
                pass
        log(f"    {last}, повтор через {wait} с ({attempt}/{MAX_RETRIES})")
        time.sleep(wait)
    raise RuntimeError(f"немає відповіді після {MAX_RETRIES} спроб: {last}")


def _as_id(value) -> int | None:
    try:
        return int(str(value).removeprefix("web:"))
    except ValueError:
        return None


def cite_order(text: str) -> list[int]:
    """Номери джерел у тому порядку, в якому відповідь на них уперше посилається."""
    order: list[int] = []
    for m in MARKER.finditer(text):
        for ref in m.group(1).split(","):
            n = _as_id(ref.strip())
            if n is not None and n not in order:
                order.append(n)
    return order


def parse_response(data: dict) -> dict:
    """Відповідь, знайдені джерела і порядок, у якому відповідь на них посилається."""
    output = data.get("output") or []
    answer = "".join(part.get("text", "") for item in output if item.get("type") == "message"
                     for part in item.get("content") or [] if part.get("type") == "output_text")
    order = cite_order(answer)
    sources, seen = [], set()
    for item in output:
        if item.get("type") != "search_results":
            continue
        for res in item.get("results") or []:
            sid = _as_id(res.get("id"))
            url = res.get("url") or ""
            if sid is None or sid in seen or not url:
                continue
            seen.add(sid)
            sources.append({"id": sid, "url": url, "domain": domain_of(url), "title": res.get("title") or "",
                            "date": res.get("date") or "", "cited": sid in order,
                            "cite_order": order.index(sid) + 1 if sid in order else None})
    cost = ((data.get("usage") or {}).get("cost") or {}).get("total_cost") or 0
    return {"answer": answer, "sources": sources, "cost": float(cost), "model": data.get("model") or "",
            "effort": data.get("effort") or "", "status": data.get("status") or ""}


# -------------------------------- журнал -----------------------------------
def journal_key(prompt: str, effort: str, country: str) -> str:
    return f"{effort}|{country}|{prompt}"


def load_journal(path: Path) -> dict[str, dict]:
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("ok"):
                done[journal_key(rec["prompt"], rec["effort_req"], rec["country"])] = rec
    return done


def append_journal(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# -------------------------------- зведення ---------------------------------
def own_status(rec: dict, own: str) -> tuple[str, int | None]:
    """cited - сайт процитовано, found - знайдено, але не процитовано, none - немає серед джерел."""
    mine = [s for s in rec["sources"] if is_own(s["domain"], own)]
    cited = sorted(s["cite_order"] for s in mine if s["cited"])
    if cited:
        return "cited", cited[0]
    return ("found" if mine else "none"), None


def summarize(records: list[dict], own: str = "") -> dict:
    doms: dict[str, dict] = defaultdict(lambda: {"cited": 0, "found": 0, "prompts": set(), "urls": Counter()})
    for rec in records:
        for s in rec["sources"]:
            if not s["domain"]:
                continue
            d = doms[s["domain"]]
            d["found"] += 1
            if s["cited"]:
                d["cited"] += 1
                d["prompts"].add(rec["prompt"])
                d["urls"][s["url"]] += 1
    total_cited = sum(d["cited"] for d in doms.values())
    rows = [{"domain": k, "cited": d["cited"], "answers": len(d["prompts"]), "found": d["found"],
             "share": round(d["cited"] / total_cited * 100, 1) if total_cited else 0.0,
             "rate": round(d["cited"] / d["found"] * 100) if d["found"] else 0,
             "urls": d["urls"].most_common()} for k, d in doms.items()]
    rows.sort(key=lambda r: (-r["cited"], -r["answers"], -r["found"], r["domain"]))
    statuses = Counter(own_status(rec, own)[0] for rec in records) if own else Counter()
    return {"rows": rows, "prompts": len(records), "found": sum(len(r["sources"]) for r in records),
            "cited": total_cited, "domains_cited": sum(1 for r in rows if r["cited"]),
            "cost": sum(r.get("cost", 0) for r in records), "own": own, "own_statuses": statuses}


# ---------------------------------- xlsx -----------------------------------
def build_xlsx(records: list[dict], summ: dict, meta: dict, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    font, bold = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True, color="FFFFFF")
    fill_hdr, fill_band = PatternFill("solid", fgColor="1F6FEB"), PatternFill("solid", fgColor="F3F6FA")
    border = Border(*[Side(style="thin", color="D9D9D9")] * 4)
    wrap, center = Alignment(wrap_text=True, vertical="top"), Alignment(horizontal="center", vertical="top")

    def sheet(ws, headers, widths, rows, centered=()):
        for i, (h, w) in enumerate(zip(headers, widths), 1):
            c = ws.cell(row=1, column=i, value=h)
            c.font, c.fill, c.border, c.alignment = bold, fill_hdr, border, Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = w
        for r, values in enumerate(rows, 2):
            for i, v in enumerate(values, 1):
                c = ws.cell(row=r, column=i, value=v)
                c.font, c.border = font, border
                c.alignment = center if i in centered else wrap
                if r % 2:
                    c.fill = fill_band
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"

    own = summ["own"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Відповіді"
    rows = []
    for n, rec in enumerate(records, 1):
        cited = [s for s in rec["sources"] if s["cited"]]
        row = [n, rec["prompt"], plain_math(rec["answer"])[:32000], len(rec["sources"]), len(cited),
               ", ".join(dict.fromkeys(s["domain"] for s in sorted(cited, key=lambda s: s["cite_order"])))]
        if own:
            row.append(OWN_LABEL[own_status(rec, own)[0]])
        rows.append(row + [rec["model"], round(rec.get("cost", 0), 5), rec["ts"]])
    headers = ["№", "Промпт", "Відповідь", "Знайдено джерел", "Процитовано", "Процитовані домени"]
    widths = [5, 40, 80, 10, 11, 45]
    if own:
        headers, widths = headers + [own], widths + [22]
    sheet(ws, headers + ["Модель", "Вартість, $", "Дата"], widths + [18, 11, 16], rows,
          centered={1, 4, 5, len(headers) + 2})

    rows = [[rec["prompt"], s["id"], "так" if s["cited"] else "ні", s["cite_order"], s["domain"], s["title"],
             s["url"], s["date"]] for rec in records for s in rec["sources"]]
    sheet(wb.create_sheet("Джерела"), ["Промпт", "ID", "Процитовано", "Порядок у відповіді", "Домен", "Заголовок",
                                       "URL", "Дата джерела"], [40, 6, 12, 12, 26, 50, 60, 13], rows, {2, 3, 4})

    rows = [[n, r["domain"], r["cited"], r["answers"], r["share"], r["found"], r["rate"],
             " · ".join(f"{u} ×{c}" for u, c in r["urls"][:8])] for n, r in enumerate(summ["rows"], 1)]
    sheet(wb.create_sheet("Домени"), ["№", "Домен", "Цитувань", "У скількох відповідях", "Частка цитувань, %",
                                      "Знайдено разів", "Цитує із знайденого, %", "Процитовані URL"],
          [5, 30, 10, 14, 14, 12, 14, 100], rows, {1, 3, 4, 5, 6, 7})

    if own:
        rows = []
        for n, rec in enumerate(records, 1):
            status, pos = own_status(rec, own)
            instead = [s["domain"] for s in sorted((s for s in rec["sources"] if s["cited"]),
                                                   key=lambda s: s["cite_order"]) if not is_own(s["domain"], own)]
            rows.append([n, rec["prompt"], OWN_LABEL[status], pos, ", ".join(list(dict.fromkeys(instead))[:5])])
        sheet(wb.create_sheet("Ваш домен"), ["№", "Промпт", f"{own}", "Місце серед цитувань", "Кого цитує замість"],
              [5, 50, 30, 12, 60], rows, {1, 4})

    checked = bool(own or summ["brands"])
    rows = []
    for n, t in enumerate((t for t in summ["targets"] if t["type"] != targets.OWN), 1):
        rows.append([n, t["score"], t["domain"], t["type"], t["answers"], t["cited"]]
                    + ([t["status"], t["proof_url"], t["snippet"]] if checked else [])
                    + [t["action"], "\n".join(u for u, _ in t["urls"][:10])])
    sheet(wb.create_sheet("Де отримати посилання", 0),
          ["№", "Пріоритет", "Домен", "Тип", "У скількох відповідях", "Цитувань"]
          + (["Ваш сайт на цих сторінках", "Де саме", "Фрагмент зі згадкою"] if checked else [])
          + ["Що робити", "Процитовані сторінки"],
          [5, 10, 28, 22, 12, 10] + ([24, 50, 60] if checked else []) + [60, 80], rows, {1, 2, 5, 6})

    rows = []
    for i, c in [("ви", summ["own_row"])] * bool(summ["own_row"]) + list(enumerate(summ["competitors"], 1)):
        rows.append([i, c["name"], c["domain"], c["type"], len(c["mentions"]) if c["mentions"] is not None else "",
                     c["answers"] if c["domain"] else "", c["cited"] if c["domain"] else "",
                     "\n".join(sorted(set(c["mentions"] or []) | set(c["prompts"]), key=[r["prompt"] for r in records].index))])
    sheet(wb.create_sheet("Конкуренти", 0), ["№", "Бренд", "Сайт", "Тип сайту", "Названо у відповідях",
                                             "Сайт серед джерел, відповідей", "Цитувань сайту", "Запити"],
          [5, 18, 26, 18, 12, 14, 10, 90], rows, {1, 5, 6, 7})

    rows = [[k, v] for k, v in brief_rows(meta)] + [["Запитів", len(records)]]
    rows += [[f"Запит {i}", rec["prompt"]] for i, rec in enumerate(records, 1)]
    sheet(wb.create_sheet("Що перевіряли", 0), ["Параметр", "Значення"], [22, 100], rows)
    wb.active = 0
    wb.save(path)


OWN_LABEL = {"cited": "процитовано", "found": "знайдено, але не процитовано", "none": "немає серед джерел"}


# ---------------------------------- HTML -----------------------------------
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#5d6672;--line:#e3e6ea;--accent:#1f6feb;--soft:#e8f0fe;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--ink:#e6e8eb;--muted:#9aa3ad;--line:#2a3038;--accent:#5b9bff;--soft:#1c2a40;--ok:#3fb950}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 48px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}
.meta{color:var(--muted);font-size:14px;margin:0 0 20px}a{color:var(--accent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}.tile b{display:block;font-size:24px}.tile span{color:var(--muted);font-size:13px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}td.n,th.n{text-align:right;white-space:nowrap}tr:last-child td{border-bottom:0}
.bar{height:8px;border-radius:4px;background:var(--accent);min-width:2px}.dom{min-width:170px;overflow-wrap:anywhere}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:0 0 10px}
summary{cursor:pointer;padding:12px 14px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}summary .q{flex:1;font-weight:600}
summary .c{color:var(--muted);font-size:13px}.body{padding:0 14px 14px}
.answer{white-space:pre-wrap;background:var(--bg);border-radius:8px;padding:12px;margin:0 0 12px;font-size:14px}
.answer sup a{text-decoration:none;font-weight:600}.chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 12px}
.chip{background:var(--soft);color:var(--ink);border-radius:999px;padding:2px 10px;font-size:12px}
.tag{font-size:12px;border-radius:6px;padding:1px 6px;margin-right:6px;white-space:nowrap}.tag.yes{background:var(--ok);color:#fff}.tag.not{border:1px solid var(--line)}
.own{border-left:4px solid var(--ok)}footer{margin-top:32px;color:var(--muted);font-size:13px}
.c{color:var(--muted);font-size:12px}.urls{font-size:12px;margin-top:4px}
.urls a{display:block;max-width:440px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ev{font-size:12px;color:var(--muted);margin-top:4px;max-width:340px}
.st{font-size:12px;border-radius:6px;padding:1px 6px;white-space:nowrap;background:var(--soft)}.st.win{background:var(--ok);color:#fff}
.brief{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 18px;background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}.brief span{display:block;color:var(--muted);font-size:12px}
.prompts ol{columns:2;column-gap:32px;margin:0;padding:0 14px 14px 36px;font-size:14px}.prompts li{break-inside:avoid;margin:0 0 4px}
.prompts{margin-bottom:16px}ul.src{list-style:none;margin:0;padding:0;font-size:14px}ul.src li{margin:0 0 6px}ul.src li.no{color:var(--muted)}
@media (max-width:640px){.prompts ol{columns:1}table.cards thead{display:none}
table.cards tr{display:grid;grid-template-columns:auto 1fr auto;gap:4px 12px;padding:12px;border-bottom:1px solid var(--line)}
table.cards tr:last-child{border-bottom:0}table.cards td{border:0;padding:0}table.cards td.dom{min-width:0}
table.cards td.wide{grid-column:1/-1}table.cards td.hide{display:none}.urls a{max-width:100%}}
"""


# LaTeX у відповідях Perplexity: \( ... \) і \[ ... \] показуються звичайним текстом
MATH = re.compile(r"\\\((.+?)\\\)|\\\[(.+?)\\\]", re.S)
MATH_NAMES = {"geq": "≥", "ge": "≥", "leq": "≤", "le": "≤", "times": "×", "cdot": "·", "approx": "≈", "sum": "Σ",
              "Delta": "Δ", "pm": "±", "neq": "≠", "to": "→", "infty": "∞"}


def plain_math(text: str) -> str:
    def conv(m):
        s = m.group(1) or m.group(2) or ""
        s = re.sub(r"\\(?:text|mathrm|textbf|operatorname)\{([^{}]*)\}", r"\1", s)
        s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
        s = s.replace("{,}", ",").replace(r"\%", "%").replace(r"\,", " ").replace(r"\;", " ").replace("\\ ", " ")
        s = re.sub(r"\\([a-zA-Z]+)", lambda k: MATH_NAMES.get(k.group(1), ""), s)
        s = re.sub(r"_\{([^{}]*)\}", r"(\1)", s).replace("{", "").replace("}", "")
        return re.sub(r"\s+", " ", s).strip()
    return MATH.sub(conv, text)


def _answer_html(text: str, anchor: str, ids: set[int] | None = None) -> str:
    """Відповідь для звіту: формули текстом, жирне і маркери з тими самими номерами, що в списку джерел.

    Маркер стає [1], якщо це перше джерело, на яке посилається відповідь. Приховані джерела лишаються без посилання."""
    order = cite_order(text)
    out = html.escape(plain_math(text), quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)

    def link(m):
        parts = []
        for ref in m.group(1).split(","):
            sid = _as_id(ref.strip())
            if sid is None or sid not in order:
                continue
            k = order.index(sid) + 1
            parts.append(f'<sup><a href="#{anchor}-{sid}">[{k}]</a></sup>' if ids is None or sid in ids
                         else f"<sup>[{k}]</sup>")
        return "".join(parts)
    return MARKER.sub(link, out)


def brief_rows(meta: dict) -> list[tuple[str, str]]:
    """Що перевіряли: бренд, сайт, конкуренти, де і як шукали."""
    where = f"Perplexity, пошук з країни {meta['country']}" if meta["country"] else "Perplexity, без прив'язки до країни"
    rows = [("Бренд", ", ".join(meta["brands"]) or "не вказано"), ("Ваш сайт", meta["own"] or "не вказано"),
            ("Конкуренти", ", ".join(meta["competitors"]) or "автоматично: магазини серед джерел"),
            ("Де шукали", where), ("Режим і дата", f"{meta['effort']} ({meta['models']}), {meta['span']}")]
    if meta["hidden"]:
        rows.append(("Приховано", f"{meta['hidden']} джерел із зон {', '.join('.' + t for t in meta['tlds'])}"))
    return rows


def build_html(records: list[dict], summ: dict, meta: dict, path: Path) -> None:
    e = html.escape
    own, n = summ["own"], summ["prompts"]
    pidx = {rec["prompt"]: i for i, rec in enumerate(records, 1)}
    brief_html = "".join(f"<div><span>{e(k)}</span><b>{e(v)}</b></div>" for k, v in brief_rows(meta))
    prompts_html = "".join(f'<li><a href="#p{i}">{e(rec["prompt"])}</a></li>' for i, rec in enumerate(records, 1))

    tiles = [("Знайдено джерел", summ["found"]), ("Процитовано у відповідях", summ["cited"]),
             ("Доменів процитовано", summ["domains_cited"]), ("Вартість прогону", f"${summ['cost']:.3f}")]
    if own:
        tiles.insert(0, (f"{own} процитовано", f"{summ['own_statuses']['cited']} з {n}"))
    cands = [t for t in summ["targets"] if t["type"] != targets.OWN]
    reachable = sum(1 for t in cands if t["type"] in (targets.MEDIA, targets.BLOG, targets.UGC))
    tiles.insert(-1, ("Медіа, блогів і форумів серед джерел", reachable))
    checked = bool(own or summ["brands"])
    if checked:
        tiles.insert(-1, ("Згадують вас без посилання", sum(1 for t in cands if t["status"] == targets.MENTIONS)))
    tiles_html = "".join(f'<div class="tile"><b>{e(str(v))}</b><span>{e(k)}</span></div>' for k, v in tiles)

    def prompt_links(prompts):
        shown = "".join(f'<a href="#p{pidx[p]}">{e(p)}</a>' for p in prompts[:3] if p in pidx)
        more = f'<span class="c">і ще {len(prompts) - 3}</span>' if len(prompts) > 3 else ""
        return f'<div class="urls">{shown}{more}</div>'

    def comp_row(c, num, cls=""):
        label = c["name"] or c["domain"]
        sub = " · ".join(x for x in (c["domain"] if c["name"] else "", c["type"]) if x)
        named = f'названо у {len(c["mentions"])} з {n}' if c["mentions"] is not None else ""
        site = (f'сайт у джерелах {c["answers"]} з {n}' if c["answers"] else "сайту серед джерел немає") if c["domain"] else ""
        prompts = sorted(set(c["mentions"] or []) | set(c["prompts"]), key=lambda p: pidx.get(p, 0))
        return (f'<tr{cls}><td class="n">{num}</td><td class="dom"><b>{e(label)}</b><div class="c">{e(sub)}</div></td>'
                f'<td>{"<br>".join(e(x) for x in (named, site) if x)}</td><td class="wide">{prompt_links(prompts)}</td></tr>')

    comp_rows = [comp_row(summ["own_row"], "", ' class="own"')] if summ["own_row"] else []
    comp_rows += [comp_row(c, i) for i, c in enumerate(summ["competitors"], 1)]
    if meta["competitors"]:
        comp_note = "Конкуренти задані параметром <code>--competitors</code>."
    else:
        comp_note = ("Автоматично: п'ять магазинів, які Perplexity цитує найчастіше. Свій список конкурентів, назвами "
                     "брендів і доменами, задайте параметром <code>--competitors</code>.")
    comp_note += (" «Названо» - у скількох відповідях Perplexity згадує бренд у тексті, «сайт у джерелах» - у скількох "
                  "відповідях сайт бренду серед процитованих джерел.")
    comp_html = (f'<div class="scroll"><table class="cards"><thead><tr><th class="n">#</th><th>Бренд і сайт</th>'
                 f'<th>Видимість у відповідях</th><th>За якими запитами</th></tr></thead>'
                 f'<tbody>{"".join(comp_rows)}</tbody></table></div>' if comp_rows
                 else '<p class="meta">Серед процитованих сайтів магазинів немає.</p>')

    names = summ["brands"] + ([own] if own else [])
    brand_rx = re.compile("(" + "|".join(re.escape(html.escape(x)) for x in names) + ")", re.I) if names else None

    def target_row(t):
        pages = "".join(f'<a href="{e(u)}" rel="nofollow noopener" target="_blank">{e(title or u)}</a>'
                        for u, title in t["urls"][:3])
        status = ""
        if checked:
            win = " win" if t["status"] == targets.MENTIONS else ""
            proof = ""
            if t["proof_url"]:
                marked = brand_rx.sub(r"<b>\1</b>", e(t["snippet"])) if brand_rx else e(t["snippet"])
                quote = f"«{marked}» " if t["snippet"] else ""
                proof = (f'<div class="ev">{quote}<a href="{e(t["proof_url"])}" rel="nofollow noopener" target="_blank">'
                         f'на цій сторінці</a></div>')
            status = f'<td class="wide"><span class="st{win}">{e(t["status"])}</span>{proof}</td>'
        return (f'<tr><td class="n">{t["score"]:g}</td><td class="dom"><b>{e(t["domain"])}</b><div class="c">{e(t["type"])}'
                f'</div></td><td class="n">{t["answers"]} з {n}</td>{status}<td class="wide">{e(t["action"])}'
                f'<div class="urls">{pages}</div></td></tr>')
    targets_html = "".join(target_row(t) for t in cands[:30])
    status_th = "<th>Ваш сайт на сторінках</th>" if checked else ""

    top = [r for r in summ["rows"] if r["cited"]][:40]
    peak = max((r["cited"] for r in top), default=1)
    rows_html = "".join(
        f'<tr{" class=own" if is_own(r["domain"], own) else ""}><td class="n">{i}</td><td class="dom">{e(r["domain"])}</td>'
        f'<td class="n">{r["cited"]}</td><td class="n">{r["answers"]} з {n}</td><td class="n">{r["share"]}%</td>'
        f'<td style="min-width:90px"><div class="bar" style="width:{r["cited"] / peak * 100:.0f}%"></div></td>'
        f'<td class="n">{r["rate"]}%</td></tr>' for i, r in enumerate(top, 1))

    items = []
    for i, rec in enumerate(records, 1):
        anchor = f"p{i}"
        cited = sorted((s for s in rec["sources"] if s["cited"]), key=lambda s: s["cite_order"])
        rest = [s for s in rec["sources"] if not s["cited"]]
        chips = "".join(f'<span class="chip">{e(d)}</span>' for d in dict.fromkeys(s["domain"] for s in cited))
        src = "".join(
            f'<li id="{anchor}-{s["id"]}"><span class="tag yes">{s["cite_order"]}</span><a href="{e(s["url"])}" '
            f'rel="nofollow noopener" target="_blank">{e(s["title"] or s["url"])}</a> <span class="c">{e(s["domain"])}</span></li>'
            for s in cited)
        src += "".join(
            f'<li id="{anchor}-{s["id"]}" class="no"><span class="tag not">не процитовано</span><a href="{e(s["url"])}" '
            f'rel="nofollow noopener" target="_blank">{e(s["title"] or s["url"])}</a> <span class="c">{e(s["domain"])}</span></li>'
            for s in rest)
        status = f' · {e(OWN_LABEL[own_status(rec, own)[0]])}' if own else ""
        items.append(
            f'<details id="{anchor}"><summary><span class="q">{i}. {e(rec["prompt"])}</span><span class="c">у відповіді '
            f'{len(cited)} з {len(rec["sources"])} знайдених джерел{status}</span></summary><div class="chips">{chips}</div>'
            f'<div class="body"><div class="answer">{_answer_html(rec["answer"], anchor, {s["id"] for s in rec["sources"]})}'
            f'</div><ul class="src">{src}</ul></div></details>')

    page = f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(meta["title"])}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{e(meta["title"])}</h1>
<div class="brief">{brief_html}</div>
<details class="prompts" open><summary><span class="q">Запити, які перевіряли: {n}</span></summary><ol>{prompts_html}</ol></details>
<div class="tiles">{tiles_html}</div>
<h2>Ваш бренд і конкуренти</h2>
{comp_html}
<p class="meta">{comp_note}</p>
<h2>Де отримати посилання</h2>
<div class="scroll"><table class="cards"><thead><tr><th class="n">Пріоритет</th><th>Домен і тип</th><th class="n">У відповідях</th>{status_th}
<th>Що робити і які сторінки цитує Perplexity</th></tr></thead><tbody>{targets_html}</tbody></table></div>
<p class="meta">Пріоритет - у скількох відповідях процитовано домен, помножене на вагу типу: медіа й блоги 1, форуми й
соцмережі 0.8, маркетплейси 0.6, інше 0.5, магазини 0.4, вікі 0.3, держсайти й наука 0.1. Якщо сторінки домену вже
згадують ваш бренд без посилання, пріоритет ×1.5, якщо вже посилаються на вас - ×0.3.</p>
<h2>Які домени цитує Perplexity</h2>
<div class="scroll"><table><thead><tr><th class="n">#</th><th>Домен</th><th class="n">Цитувань</th><th class="n">У відповідях</th>
<th class="n">Частка</th><th></th><th class="n">Цитує із знайденого</th></tr></thead><tbody>{rows_html}</tbody></table></div>
<p class="meta">«Цитує із знайденого» - яку частку сторінок домену, знайдених пошуком, відповідь справді взяла за джерело.</p>
<h2>Відповіді та джерела</h2>
<p class="meta">На кожен запит пошук Perplexity знаходить кілька сторінок, а відповідь посилається лише на частину з них.
Зелений номер - відповідь послалася на цю сторінку, він збігається з номером у тексті відповіді. «Не процитовано» -
сторінку знайдено, але у відповідь вона не потрапила.</p>
{"".join(items)}
<footer>Звіт зроблено <a href="{REPO_URL}">perplexity-citations</a>. Відповіді Perplexity змінюються від запуску до запуску.</footer>
</div><script>function op(){{var d=document.getElementById(location.hash.slice(1));if(d&&d.tagName=="DETAILS")d.open=true}}
addEventListener("hashchange",op);op()</script></body></html>
"""
    path.write_text(page, encoding="utf-8", newline="\n")


# ---------------------------------- запуск ---------------------------------
def report_meta(records: list[dict], effort: str, country: str, own: str = "", brands: list[str] = (),
                competitors: list[str] = (), hidden: int = 0, tlds: list[str] = ()) -> dict:
    dates = sorted(r["ts"][:10] for r in records)
    span = dates[0] if dates[0] == dates[-1] else f"{dates[0]} - {dates[-1]}"
    models = ", ".join(sorted({r["model"] for r in records if r["model"]}))
    return {"title": "Що цитує Perplexity", "span": span, "models": models, "effort": effort, "country": country,
            "own": own, "brands": list(brands), "competitors": list(competitors), "hidden": hidden, "tlds": list(tlds)}


def parse_rivals(arg: str) -> list[dict]:
    """--competitors "Bluetti,Fossibot=fossi-bot.com.ua,shop.ua": назва бренду, назва з доменом або лише домен."""
    out = []
    for part in (p.strip() for p in arg.split(",")):
        if not part:
            continue
        name, _, dom = (x.strip() for x in part.partition("="))
        if not dom and "." in name and " " not in name:
            name, dom = "", name
        dom = domain_of("https://" + dom.removeprefix("https://").removeprefix("http://")) if dom else ""
        out.append({"name": name, "domain": dom})
    return out


def mentioned_in(records: list[dict], names: list[str]) -> list[str]:
    """Запити, у відповідях на які названо бренд (будь-яку з його назв)."""
    rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(x) for x in names) + r")(?![\w-])", re.I)
    return [r["prompt"] for r in records if rx.search(r["answer"])]


def rival_rows(records: list[dict], target_rows: list[dict], rivals: list[dict], own: str,
               brands: list[str]) -> tuple[list[dict], dict | None]:
    """Конкуренти і ваш рядок для порівняння: скільки разів бренд названо у відповідях і як часто його сайт серед джерел."""
    cands = [t for t in target_rows if t["type"] != targets.OWN]
    nothing = {"type": "", "answers": 0, "cited": 0, "prompts": []}
    if rivals:
        rows = []
        for r in rivals:
            site = targets.competitors(cands, [r["domain"]])[0] if r["domain"] else nothing
            rows.append({**site, "name": r["name"], "domain": r["domain"],
                         "mentions": mentioned_in(records, [r["name"]]) if r["name"] else None})
        rows.sort(key=lambda c: (-len(c["mentions"] or []), -c["answers"], c["name"] or c["domain"]))
    else:
        rows = [{**c, "name": "", "mentions": None} for c in targets.competitors(cands)]
    own_row = None
    if own or brands:
        mine = [rec["prompt"] for rec in records if own and own_status(rec, own)[0] == "cited"]
        own_row = {"name": brands[0] if brands else "", "domain": own, "type": "ваш бренд", "answers": len(mine),
                   "cited": sum(1 for rec in records for s in rec["sources"] if s["cited"] and is_own(s["domain"], own)),
                   "prompts": mine, "mentions": mentioned_in(records, brands) if brands else None}
    return rows, own_row


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Які джерела цитує Perplexity за вашими промптами")
    ap.add_argument("input", nargs="?", help="txt (промпт на рядок), xlsx або csv (перша колонка)")
    ap.add_argument("--domain", default="", help="ваш домен: показати, де його цитують, а де ні")
    ap.add_argument("--brand", default="", help="назви бренду через кому: шукати згадки на процитованих сторінках")
    ap.add_argument("--competitors", default="", help="домени конкурентів через кому; без нього - магазини серед джерел")
    ap.add_argument("--no-fetch", action="store_true", help="не завантажувати процитовані сторінки: тип сайту лише за адресою")
    ap.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORTS, help="глибина пошуку (за замовчуванням fast)")
    ap.add_argument("--country", default=DEFAULT_COUNTRY, help='код країни пошуку, "" - без прив\'язки')
    ap.add_argument("--domain-filter", default="", help="шукати лише на цих доменах, через кому; -домен виключає")
    ap.add_argument("--recency", default="", choices=("", "hour", "day", "week", "month", "year"),
                    help="лише свіжі джерела")
    ap.add_argument("--exclude-tld", default="", help="не показувати у звітах джерела з цих зон, через кому: ru,by")
    ap.add_argument("--limit", type=int, default=0, help="лише перші N промптів")
    ap.add_argument("--out-dir", default="results", help="куди писати журнал і звіти (за замовчуванням results)")
    ap.add_argument("--fresh", action="store_true", help="прогнати промпти заново, не беручи відповіді з журналу")
    ap.add_argument("--report-only", action="store_true", help="лише перебудувати xlsx і HTML з журналу")
    args = ap.parse_args()

    own = domain_of("https://" + args.domain.strip().removeprefix("https://").removeprefix("http://")) if args.domain else ""
    country = args.country.strip().upper()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    journal_path = out_dir / "journal.jsonl"

    if not args.input and not args.report_only:
        ap.error("вкажіть файл із промптами, наприклад: python citations.py prompts.example.txt")
    prompts = load_prompts(Path(args.input)) if args.input else []
    if args.limit:
        prompts = prompts[:args.limit]

    done = load_journal(journal_path)
    if not args.report_only:
        load_env(HERE / ".env")
        api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
        if not api_key:
            sys.exit("[!] Немає ключа PERPLEXITY_API_KEY. Скопіюйте .env.example у .env і впишіть ключ "
                     "(console.perplexity.ai/project/keys).")
        if not prompts:
            sys.exit("[!] У файлі немає жодного промпту.")
        todo = [p for p in prompts if args.fresh or journal_key(p, args.effort, country) not in done]
        log(f"Промптів: {len(prompts)} | уже в журналі: {len(prompts) - len(todo)} | до прогону: {len(todo)}")
        spent = 0.0
        domain_filter = [d.strip() for d in args.domain_filter.split(",") if d.strip()]
        for i, prompt in enumerate(todo, 1):
            log(f"[{i}/{len(todo)}] {prompt[:80]}")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            try:
                rec = parse_response(call_api(build_body(prompt, args.effort, country, domain_filter, args.recency),
                                              api_key))
            except Exception as e:
                append_journal(journal_path, {"ok": False, "prompt": prompt, "error": str(e), "ts": ts})
                log(f"    [помилка] {e}")
            else:
                rec.update(ok=True, prompt=prompt, effort_req=args.effort, country=country, ts=ts)
                append_journal(journal_path, rec)
                done[journal_key(prompt, args.effort, country)] = rec
                spent += rec["cost"]
                log(f"    знайдено {len(rec['sources'])}, процитовано {sum(s['cited'] for s in rec['sources'])}, "
                    f"${rec['cost']:.4f}")
            if i < len(todo):
                time.sleep(PAUSE_BETWEEN)
        log(f"Витрачено за цей запуск: ${spent:.4f}")

    if prompts:
        records = [done[k] for k in (journal_key(p, args.effort, country) for p in prompts) if k in done]
    else:
        records = [r for r in done.values() if r["effort_req"] == args.effort and r["country"] == country]
    if not records:
        sys.exit("[!] Немає жодної успішної відповіді в журналі, звіти не створено.")
    tlds = [t.strip().lower().lstrip(".") for t in args.exclude_tld.split(",") if t.strip()]
    records, hidden = hide_tlds(records, tlds)
    summ = summarize(records, own)
    brands = [b.strip() for b in args.brand.split(",") if b.strip()]
    pages = {}
    if not args.no_fetch:
        urls = list(dict.fromkeys(s["url"] for r in records for s in r["sources"]
                                  if s["cited"] and not is_own(s["domain"], own)))
        log(f"Відкриваю процитовані сторінки ({len(urls)}), щоб визначити тип сайтів"
            + (" і знайти згадки бренду" if own or brands else "") + "...")
        pages = targets.Fetcher(out_dir / "pages.json").fetch_all(urls)
        failed = sum(1 for p in pages.values() if not p.get("ok"))
        if failed:
            log(f"    не вдалося відкрити {failed} з {len(urls)}: тип для них визначено за адресою")
    summ["targets"], summ["brands"] = targets.link_targets(records, pages, own, brands), brands
    rivals = parse_rivals(args.competitors)
    summ["competitors"], summ["own_row"] = rival_rows(records, summ["targets"], rivals, own, brands)
    meta = report_meta(records, args.effort, country, own, brands,
                       [f'{r["name"]} ({r["domain"]})' if r["name"] and r["domain"] else r["name"] or r["domain"]
                        for r in rivals], hidden, tlds)
    stamp = datetime.now().strftime("%Y-%m-%d")
    xlsx_path, html_path = out_dir / f"citations_{stamp}.xlsx", out_dir / "report.html"
    build_xlsx(records, summ, meta, xlsx_path)
    build_html(records, summ, meta, html_path)
    if hidden:
        log(f"Приховано джерел із зон {', '.join(tlds)}: {hidden}")
    log(f"Готово. Відповідей: {len(records)}, процитовано джерел: {summ['cited']}, доменів: {summ['domains_cited']}.")
    log(f"xlsx:  {xlsx_path}\nHTML:  {html_path}")


if __name__ == "__main__":
    main()
