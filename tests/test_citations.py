import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import citations as c  # noqa: E402

RESPONSE = {
    "model": "openai/test-model",
    "effort": "low@2026-11-11.0",
    "status": "completed",
    "output": [
        {"type": "search_results", "queries": ["q1"], "results": [
            {"id": 1, "url": "https://www.alpha.com.ua/a", "title": "A", "date": "2026-10-01"},
            {"id": 2, "url": "https://beta.com/b", "title": "B", "date": ""},
            {"id": 3, "url": "https://shop.alpha.com.ua/c", "title": "C"},
        ]},
        {"type": "search_results", "queries": ["q2"], "results": [
            {"id": 4, "url": "https://gamma.org/d", "title": "D"},
            {"id": 2, "url": "https://beta.com/b", "title": "B"},
        ]},
        {"type": "message", "content": [
            {"type": "output_text", "text": "Перше **важливе** [web:4]. Друге [web:2][web:4]. Третє [1, 9].", "annotations": []},
        ]},
    ],
    "usage": {"cost": {"total_cost": 0.0021}},
}


def test_parse_response_marks_cited_sources_in_order():
    rec = c.parse_response(RESPONSE)
    by_id = {s["id"]: s for s in rec["sources"]}
    assert sorted(by_id) == [1, 2, 3, 4]
    assert by_id[4]["cite_order"] == 1 and by_id[2]["cite_order"] == 2 and by_id[1]["cite_order"] == 3
    assert not by_id[3]["cited"] and by_id[3]["cite_order"] is None
    assert by_id[1]["domain"] == "alpha.com.ua"
    assert rec["cost"] == 0.0021 and rec["model"] == "openai/test-model"


def test_fast_style_markers():
    data = {"output": [
        {"type": "search_results", "results": [{"id": 1, "url": "https://a.com"}, {"id": 2, "url": "https://b.com"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "Текст [2][1]."}]},
    ]}
    rec = c.parse_response(data)
    assert [s["cite_order"] for s in rec["sources"]] == [2, 1]


def test_summary_and_own_domain():
    rec = c.parse_response(RESPONSE)
    rec.update(prompt="p", ts="2026-11-11 10:00")
    summ = c.summarize([rec], own="alpha.com.ua")
    top = summ["rows"][0]
    assert summ["cited"] == 3 and summ["found"] == 4 and summ["domains_cited"] == 3
    rates = {r["domain"]: r["rate"] for r in summ["rows"]}
    assert rates["alpha.com.ua"] == 100 and rates["shop.alpha.com.ua"] == 0
    assert top["cited"] == 1 and top["share"] == 33.3
    assert c.own_status(rec, "alpha.com.ua") == ("cited", 3)
    assert c.own_status(rec, "beta.com") == ("cited", 2)
    assert c.own_status(rec, "zeta.com")[0] == "none"


def test_hide_tlds_keeps_journal_record_intact():
    rec = c.parse_response({"output": [{"type": "search_results", "results": [
        {"id": 1, "url": "https://a.com.ua"}, {"id": 2, "url": "https://b.ru"}, {"id": 3, "url": "https://x.sub.by"}]}]})
    shown, hidden = c.hide_tlds([rec], ["ru", "by"])
    assert hidden == 2 and [s["domain"] for s in shown[0]["sources"]] == ["a.com.ua"]
    assert len(rec["sources"]) == 3
    assert "<sup>[2]</sup>" in c._answer_html("x [2][1]", "p1", {1})


def test_classify_by_lists_url_and_markup():
    import targets as t
    assert t.classify("uk.wikipedia.org", "https://uk.wikipedia.org/wiki/X", None) == t.WIKI
    assert t.classify("rozetka.com.ua", "https://rozetka.com.ua/ua/x/p1/", None) == t.MARKET
    assert t.classify("youtube.com", "https://youtube.com/watch?v=1", None) == t.UGC
    assert t.classify("dsns.gov.ua", "https://dsns.gov.ua/news", None) == t.OFFICIAL
    assert t.classify("24tv.ua", "https://24tv.ua/a", None) == t.MEDIA
    assert t.classify("shop.example", "https://shop.example/a", None) == t.SHOP
    page = {"ok": True, "types": ["blogposting", "organization"], "og": "", "cart": False}
    assert t.classify("example.com.ua", "https://example.com.ua/a", page) == t.BLOG
    assert t.classify("example.com.ua", "https://example.com.ua/a", {**page, "cart": True}) == t.SHOP
    assert t.classify("my.com.ua", "https://blog.my.com.ua/a", None, own="my.com.ua") == t.OWN


def test_page_parser_and_mentions():
    import targets as t
    html_text = ('<html><head><meta property="og:type" content="article"><script type="application/ld+json">'
                 '{"@graph":[{"@type":"BlogPosting"},{"@type":["Organization"]}]}</script><style>.a{}</style></head>'
                 '<body><p>Огляд: бренд Volta Power.</p><a href="https://www.mybrand.com.ua/x">x</a>'
                 '<button class="add-to-cart">ok</button></body></html>')
    p = t.parse_page("https://blog.example/a", html_text)
    assert p["types"] == ["blogposting", "organization"] and p["og"] == "article" and p["cart"]
    assert "mybrand.com.ua" in p["hosts"] and "Volta Power" in p["text"] and ".a{}" not in p["text"]
    assert t.mention_status([p], "mybrand.com.ua", []) == t.LINKS
    assert t.mention_status([p], "other.ua", ["Volta Power"]) == t.MENTIONS
    assert t.mention_status([p], "other.ua", ["Volt"]) == t.ABSENT
    assert t.mention_status([{"ok": False}], "other.ua", []) == t.UNKNOWN
    assert t.mention_status([p], "", []) == ""


def test_mentions_only_in_main_text():
    import targets as t
    body = "Текст статті без згадок. " * 30
    html_text = (f'<nav>Volta Power</nav><a href="https://mybrand.ua">меню</a><article><p>{body}</p></article>'
                 '<footer>Volta Power</footer>')
    p = t.parse_page("https://news.example/a", html_text)
    assert t.mention_status([p], "mybrand.ua", ["Volta Power"]) == t.ABSENT


def test_link_targets_priority():
    import targets as t
    rec = {"prompt": "p1", "sources": [
        {"cited": True, "domain": "24tv.ua", "url": "https://24tv.ua/a", "title": "A"},
        {"cited": True, "domain": "shop.example", "url": "https://shop.example/product/1", "title": "B"},
        {"cited": False, "domain": "blog.example", "url": "https://blog.example/x", "title": "C"}]}
    rec2 = {**rec, "prompt": "p2"}
    rows = t.link_targets([rec, rec2], {})
    assert [r["domain"] for r in rows] == ["24tv.ua", "shop.example"]
    assert rows[0]["score"] == 2.0 and rows[1]["score"] == 0.8 and rows[0]["status"] == ""


def test_load_prompts_txt(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("# коментар\nПромпт\nперший\n\nдругий\nперший\n", encoding="utf-8")
    assert c.load_prompts(f) == ["перший", "другий"]


def test_answer_html_links_markers():
    out = c._answer_html("a **b** [web:2][3] <x>", "p1")
    assert "<b>b</b>" in out and 'href="#p1-2"' in out and 'href="#p1-3"' in out and "&lt;x&gt;" in out
