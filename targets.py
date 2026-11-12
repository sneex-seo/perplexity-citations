# -*- coding: utf-8 -*-
"""Де отримати посилання: тип кожного процитованого домену, що з ним робити і пріоритет.

Тип визначається за списками відомих сайтів, адресою сторінки і, якщо сторінки завантажено, за їхньою розміткою
schema.org та ознаками інтернет-магазину. З --domain або --brand сторінки ще перевіряються на посилання на ваш сайт
і на згадку бренду без посилання.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

BOT = "perplexity-citations"
UA = f"Mozilla/5.0 (compatible; {BOT}/1.0; +https://github.com/sneex-seo/perplexity-citations)"
FETCH_TIMEOUT = 10
FETCH_THREADS = 8
MAX_BYTES = 3_000_000
TEXT_LIMIT = 60_000

MEDIA, BLOG, UGC, MARKET, SHOP, WIKI, OFFICIAL, OWN, OTHER = (
    "медіа", "блог / огляд", "форум / соцмережа / відео", "маркетплейс / агрегатор", "магазин", "вікі",
    "держ. / освіта / наука", "ваш сайт", "інше")

# Наскільки реально отримати з домену посилання або згадку
WEIGHT = {MEDIA: 1.0, BLOG: 1.0, UGC: 0.8, MARKET: 0.6, SHOP: 0.4, WIKI: 0.3, OTHER: 0.5, OFFICIAL: 0.1, OWN: 0.0}
ACTION = {
    MEDIA: "PR: експертний коментар, новина або колонка з посиланням",
    BLOG: "Аутріч: гостьова стаття або місце в огляді чи добірці",
    UGC: "Відповідь у темі, огляд або відео від імені бренду чи експерта",
    MARKET: "Картка товару або лістинг: присутність серед джерел, а не посилання",
    SHOP: "Якщо ви бренд - партнерство: картка товару, згадка в статті. Якщо магазин - конкурент: вивчіть процитовану сторінку",
    WIKI: "Правка з посиланням на першоджерело за правилами вікі, без реклами",
    OFFICIAL: "Донором не стане: посилайтеся на нього у своєму контенті",
    OWN: "Утримувати: оновлювати процитовані сторінки",
    OTHER: "Перевірте вручну",
}
LINKS, MENTIONS, ABSENT, UNKNOWN = "посилається на вас", "згадує бренд без посилання", "вас немає", "не вдалося перевірити"
STATUS_FACTOR = {LINKS: 0.3, MENTIONS: 1.5, ABSENT: 1.0, UNKNOWN: 1.0, "": 1.0}

MARKETPLACES = ("rozetka.com.ua", "prom.ua", "epicentrk.ua", "allo.ua", "olx.ua", "kasta.ua", "hotline.ua", "ek.ua",
                "price.ua", "makeup.com.ua", "bigl.ua", "shafa.ua", "amazon.", "ebay.", "aliexpress.", "etsy.com",
                "temu.com")
UGC_SITES = ("reddit.com", "quora.com", "youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com",
             "t.me", "x.com", "twitter.com", "threads.net", "linkedin.com", "pinterest.", "medium.com",
             "stackexchange.com", "stackoverflow.com", "tripadvisor.", "trustpilot.com", "otzyvua.net")
WIKIS = ("wikipedia.org", "wikiwand.com", "wiktionary.org", "britannica.com", "fandom.com", "wikihow.com")
SCIENCE = ("ncbi.nlm.nih.gov", "pubmed.", "sciencedirect.com", "springer.com", "nature.com", "mdpi.com",
           "researchgate.net", "doi.org", "arxiv.org", "frontiersin.org", "wiley.com", "jamanetwork.com", "bmj.com",
           "thelancet.com", "who.int", "europa.eu", "un.org")
MEDIA_SITES = ("pravda.com.ua", "epravda.com.ua", "unian.ua", "unian.net", "24tv.ua", "rbc.ua", "focus.ua", "tsn.ua",
               "nv.ua", "liga.net", "suspilne.media", "hromadske.ua", "ukrinform.ua", "censor.net", "zn.ua",
               "glavcom.ua", "obozrevatel.com", "fakty.com.ua", "telegraf.com.ua", "unn.ua", "radiosvoboda.org",
               "forbes.ua", "ain.ua", "dev.ua", "thepage.ua", "minfin.com.ua", "finance.ua", "1plus1.ua", "ictv.ua",
               "stb.ua", "novyny.live", "babel.ua", "lb.ua", "espreso.tv", "dsnews.ua", "uatv.ua", "kyivindependent.com",
               "bbc.", "dw.com", "reuters.com", "apnews.com", "nytimes.com", "theguardian.com", "forbes.com",
               "techcrunch.com", "theverge.com", "wired.com", "cnn.com", "bloomberg.com", "businessinsider.com")
OFFICIAL_TLD = re.compile(r"(^|\.)(gov|edu|mil)(\.[a-z]{2})?$|\.gov\.ua$|\.edu\.ua$|(^|\.)int$")
PRODUCT_PATH = re.compile(r"/(product|products|tovar|goods|item|p)/|/catalog|/shop/|/kupit|add-to-cart|[?&]product_id=",
                          re.I)
SHOP_WORDS = re.compile(r"shop|store|market|magazin|\bbuy\b", re.I)
# Лише ознаки справжнього кошика: стилі WooCommerce бувають і на новинних сайтах без магазину
CART = re.compile(r"add[-_]?to[-_]?cart|addtocart|data-product[-_]id|до кошика|у кошик|в кошик|купити в один клік", re.I)
ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "reportagenewsarticle", "analysisnewsarticle", "review",
                 "howto", "techarticle", "opinionnewsarticle"}
# Без простого Offer: його ставлять і безкоштовним калькуляторам та застосункам
SHOP_TYPES = {"product", "productgroup", "aggregateoffer", "offercatalog", "store", "onlinestore", "electronicsstore",
              "hardwarestore", "homegoodsstore"}
SHOPS = ("elmir.ua", "citrus.ua", "foxtrot.com.ua", "comfy.ua", "eldorado.ua", "moyo.ua", "brain.com.ua",
         "telemart.ua", "ktc.ua", "jabko.ua", "itbox.ua", "fua.com.ua")


def _matches(domain: str, names: tuple[str, ...]) -> bool:
    return any(domain == n or domain.endswith("." + n) or (n.endswith(".") and (domain.startswith(n) or "." + n in domain))
               for n in names)


# ------------------------------ сторінки -----------------------------------
class _Page(HTMLParser):
    """Посилання, JSON-LD, og:type і видимий текст сторінки без сторонніх бібліотек."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.hosts, self.ld, self.og, self.text, self.main = base, set(), [], "", [], []
        self.main_hosts: set[str] = set()
        self._skip, self._ld, self._in_main = 0, None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("article", "main"):
            self._in_main += 1
        if tag == "a" and a.get("href"):
            host = (urlparse(urljoin(self.base, a["href"])).hostname or "").lower()
            if host:
                host = host[4:] if host.startswith("www.") else host
                self.hosts.add(host)
                if self._in_main:
                    self.main_hosts.add(host)
        elif tag == "meta" and (a.get("property") or "").lower() == "og:type":
            self.og = (a.get("content") or "").lower()
        elif tag == "script":
            self._skip += 1
            if (a.get("type") or "").lower() == "application/ld+json":
                self._ld = []
        elif tag in ("style", "noscript", "svg"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("article", "main"):
            self._in_main = max(0, self._in_main - 1)
        if tag == "script":
            if self._ld is not None:
                self.ld.append("".join(self._ld))
                self._ld = None
            self._skip = max(0, self._skip - 1)
        elif tag in ("style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        elif not self._skip and data.strip():
            self.text.append(data.strip())
            if self._in_main:
                self.main.append(data.strip())


def _ld_types(blocks: list[str]) -> list[str]:
    found = set()

    def walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            for v in t if isinstance(t, list) else [t]:
                if isinstance(v, str):
                    found.add(v.lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return sorted(found)


def parse_page(url: str, html_text: str) -> dict:
    p = _Page(url)
    try:
        p.feed(html_text)
    except Exception:
        pass
    # Згадки й посилання шукаємо в основному тексті (article або main), щоб не рахувати меню і футер.
    main = " ".join(p.main)
    use_main = len(main) >= 500
    text = main if use_main else " ".join(p.text)
    return {"ok": True, "types": _ld_types(p.ld), "og": p.og, "cart": bool(CART.search(html_text)),
            "hosts": sorted(p.main_hosts if use_main else p.hosts), "text": text[:TEXT_LIMIT]}


class Fetcher:
    """Завантажує сторінки з повагою до robots.txt і кешем на диску."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        self.robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.lock = threading.Lock()

    def _allowed(self, url: str) -> bool:
        u = urlparse(url)
        root = f"{u.scheme}://{u.netloc}"
        with self.lock:
            rp = self.robots.get(root, False)
        if rp is False:
            rp = robotparser.RobotFileParser()
            try:
                r = requests.get(root + "/robots.txt", headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                rp.disallow_all = r.status_code >= 500  # RFC 9309: сервер недоступний - вважаємо, що заборонено все
            except requests.RequestException:
                rp = None
            with self.lock:
                self.robots[root] = rp
        return rp is None or rp.can_fetch(BOT, url)

    def _get(self, url: str) -> dict:
        if not self._allowed(url):
            return {"ok": False, "error": "robots.txt"}
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "uk,en;q=0.8"}, timeout=FETCH_TIMEOUT,
                             stream=True)
            body = r.raw.read(MAX_BYTES, decode_content=True)
        except requests.RequestException as e:
            return {"ok": False, "error": type(e).__name__}
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200 or "html" not in ctype:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4000], re.I)
        enc = r.encoding if "charset" in ctype.lower() else (meta.group(1).decode() if meta else "utf-8")
        try:
            text = body.decode(enc, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return parse_page(r.url, text)

    def fetch_all(self, urls: list[str]) -> dict[str, dict]:
        todo = [u for u in dict.fromkeys(urls) if u not in self.cache]
        if todo:
            with ThreadPoolExecutor(FETCH_THREADS) as pool:
                for url, page in zip(todo, pool.map(self._get, todo)):
                    self.cache[url] = page
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        return {u: self.cache[u] for u in urls}


# ------------------------------ класифікація --------------------------------
def classify(domain: str, url: str, page: dict | None, own: str = "") -> str:
    if own and (domain == own or domain.endswith("." + own)):
        return OWN
    if _matches(domain, WIKIS):
        return WIKI
    if _matches(domain, UGC_SITES):
        return UGC
    if _matches(domain, MARKETPLACES):
        return MARKET
    if OFFICIAL_TLD.search(domain) or _matches(domain, SCIENCE):
        return OFFICIAL
    types = set(page.get("types", [])) if page and page.get("ok") else set()
    if _matches(domain, MEDIA_SITES):
        return MEDIA
    if (_matches(domain, SHOPS) or types & SHOP_TYPES or (page and page.get("cart")) or PRODUCT_PATH.search(url)
            or SHOP_WORDS.search(domain)):
        return SHOP
    if types & {"newsarticle", "newsmediaorganization", "reportagenewsarticle"}:
        return MEDIA
    if types & ARTICLE_TYPES or (page and page.get("og") == "article") or re.search(r"/(blog|news|novyny|articles?|"
                                                                                     r"statti|journal)/", url, re.I):
        return BLOG
    return OTHER


def mention_status(pages: list[dict], own: str, brands: list[str]) -> str:
    """Найкращий статус серед сторінок домену: посилання, згадка без посилання, немає або невідомо."""
    if not own and not brands:
        return ""
    terms = [b.lower() for b in brands] + ([own] if own else [])
    rx = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w-])", re.I)
    checked = [p for p in pages if p and p.get("ok")]
    if own and any(h == own or h.endswith("." + own) for p in checked for h in p["hosts"]):
        return LINKS
    if any(rx.search(p["text"]) for p in checked):
        return MENTIONS
    return ABSENT if checked else UNKNOWN


def link_targets(records: list[dict], pages: dict[str, dict], own: str = "", brands: list[str] = ()) -> list[dict]:
    """Процитовані домени з типом, дією, статусом бренду і пріоритетом, від найважливіших."""
    by_dom: dict[str, dict] = {}
    for rec in records:
        for s in rec["sources"]:
            if not s["cited"] or not s["domain"]:
                continue
            d = by_dom.setdefault(s["domain"], {"domain": s["domain"], "cited": 0, "prompts": {}, "urls": {}})
            d["cited"] += 1
            d["prompts"][rec["prompt"]] = None
            d["urls"].setdefault(s["url"], s["title"])
    out = []
    for d in by_dom.values():
        urls = list(d["urls"])
        dom_pages = [pages.get(u) for u in urls]
        votes = [classify(d["domain"], u, p, own) for u, p in zip(urls, dom_pages)]
        kind = max(set(votes), key=lambda k: (votes.count(k), -list(WEIGHT).index(k)))
        status = "" if kind == OWN else mention_status(dom_pages, own, list(brands))
        answers = len(d["prompts"])
        score = round(answers * WEIGHT[kind] * STATUS_FACTOR[status], 2)
        action = ACTION[kind]
        if status == MENTIONS:
            action = "Швидкий виграш: попросіть додати посилання на згадку. " + action
        out.append({"domain": d["domain"], "type": kind, "cited": d["cited"], "answers": answers, "score": score,
                    "status": status, "action": action, "urls": list(d["urls"].items()), "prompts": list(d["prompts"])})
    out.sort(key=lambda r: (-r["score"], -r["answers"], -r["cited"], r["domain"]))
    return out


def competitors(rows: list[dict], names: list[str] = (), limit: int = 5) -> list[dict]:
    """Конкуренти серед джерел: заданий список доменів або магазини, які Perplexity цитує найчастіше."""
    by_reach = sorted(rows, key=lambda r: (-r["answers"], -r["cited"], r["domain"]))
    if not names:
        return [r for r in by_reach if r["type"] == SHOP][:limit]
    out = []
    for name in names:
        hits = [r for r in by_reach if r["domain"] == name or r["domain"].endswith("." + name)]
        prompts = list(dict.fromkeys(p for r in hits for p in r["prompts"]))
        out.append({"domain": name, "type": hits[0]["type"] if hits else "", "answers": len(prompts),
                    "cited": sum(r["cited"] for r in hits), "prompts": prompts})
    return sorted(out, key=lambda r: (-r["answers"], -r["cited"], r["domain"]))
