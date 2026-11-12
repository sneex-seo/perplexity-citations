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
    out = c._answer_html("x [2][1]", "p1", {1})
    assert "<sup>[1]</sup>" in out and 'href="#p1-1">[2]</a>' in out


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
    status, url, snippet = t.brand_check([("https://a.ua/1", {"ok": True, "hosts": [], "text": "Без бренду."}),
                                          ("https://a.ua/2", p)], "other.ua", ["Volta Power"])
    assert status == t.MENTIONS and url == "https://a.ua/2" and "Volta Power" in snippet


def test_mentions_only_in_main_text():
    import targets as t
    body = "Текст статті без згадок. " * 30
    html_text = (f'<nav>Volta Power</nav><a href="https://mybrand.ua">меню</a><article><p>{body}</p></article>'
                 '<footer>Volta Power</footer>')
    p = t.parse_page("https://news.example/a", html_text)
    assert t.mention_status([p], "mybrand.ua", ["Volta Power"]) == t.ABSENT


def test_mentions_ignore_product_blocks_and_link_lists():
    import targets as t
    body = "Текст статті без згадок. " * 30
    html_text = (f'<main><article><p>{body}</p><div class="related-products"><p>Volta Power X1</p></div>'
                 '<ul><li><a href="/x">Volta Power X2</a></li></ul><p>Читайте <a href="/y">про</a> інше.</p>'
                 '</article></main>')
    p = t.parse_page("https://shop.example/a", html_text)
    assert t.mention_status([p], "", ["Volta Power"]) == t.ABSENT and "про" in p["text"]
    ld = '{"@type":"Article","articleBody":"<p>' + "Огляд. " * 100 + 'Беріть <b>Volta Power</b>.</p>"}'
    p = t.parse_page("https://news.example/a", f'<script type="application/ld+json">{ld}</script><main>{body}</main>')
    assert t.mention_status([p], "", ["Volta Power"]) == t.MENTIONS and "<p>" not in p["text"]


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
    assert "<b>b</b>" in out and "&lt;x&gt;" in out
    # номер у тексті - порядок цитування, як і в списку джерел; посилання веде на справжнє джерело
    assert 'href="#p1-2">[1]</a>' in out and 'href="#p1-3">[2]</a>' in out


def test_rivals_by_name_and_domain():
    assert c.parse_rivals("Bluetti, Fossibot=https://www.fossi-bot.com.ua/, shop.ua") == [
        {"name": "Bluetti", "domain": ""}, {"name": "Fossibot", "domain": "fossi-bot.com.ua"},
        {"name": "", "domain": "shop.ua"}]
    recs = [{"prompt": "p1", "answer": "Беріть Bluetti або EcoFlow.", "sources": [
                {"cited": True, "domain": "fossi-bot.com.ua", "url": "https://fossi-bot.com.ua/a", "title": ""}]},
            {"prompt": "p2", "answer": "Bluetti-подібні станції", "sources": []}]
    assert c.mentioned_in(recs, ["bluetti"]) == ["p1"]
    import targets as t
    rows, mine = c.rival_rows(recs, t.link_targets(recs, {}), c.parse_rivals("Bluetti,Fossibot=fossi-bot.com.ua"),
                              "ecoflow.ua", ["EcoFlow"])
    assert [(r["name"], len(r["mentions"]), r["answers"]) for r in rows] == [("Bluetti", 1, 0), ("Fossibot", 0, 1)]
    assert mine["mentions"] == ["p1"] and mine["answers"] == 0


def test_plain_math():
    raw = r"Формула: \(P_{\text{станції}} \geq (\sum P_{\text{приладів}} + P_{\text{піку}}) \times 1{,}2\text{–}1{,}3\)"
    assert c.plain_math(raw) == "Формула: P(станції) ≥ (Σ P(приладів) + P(піку)) × 1,2–1,3"
    assert c.plain_math(r"\(E \approx (P \times t)/0{,}8\)") == "E ≈ (P × t)/0,8"
    assert c.plain_math(r"\[\frac{a}{b} \leq 1\]") == "(a)/(b) ≤ 1"


def test_competitors_auto_and_given():
    import targets as t
    rows = [{"domain": d, "type": k, "answers": a, "cited": a, "prompts": [f"p{i}" for i in range(a)]}
            for d, k, a in (("media.ua", t.MEDIA, 9), ("shop-a.ua", t.SHOP, 3), ("shop-b.ua", t.SHOP, 5),
                            ("blog.shop-c.ua", t.BLOG, 2))]
    assert [r["domain"] for r in t.competitors(rows)] == ["shop-b.ua", "shop-a.ua"]
    given = t.competitors(rows, ["shop-c.ua", "none.ua"])
    assert [(r["domain"], r["answers"]) for r in given] == [("shop-c.ua", 2), ("none.ua", 0)]
    assert given[1]["type"] == ""
