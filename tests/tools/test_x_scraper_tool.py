import json

from tools import x_scraper_tool as xst


def test_clean_jina_content_removes_x_chrome():
    raw = """Title: Example / X

## Post

## Conversation

Real tweet text with signal.

·

120

## New to X?

Sign up
"""

    assert xst._clean_jina_content(raw) == "Real tweet text with signal."


def test_scrape_one_prefers_external_link_over_short_jina(monkeypatch):
    monkeypatch.setattr(xst, "_scrape_via_xurl", lambda url: None)
    monkeypatch.setattr(
        xst,
        "_scrape_via_fxtwitter",
        lambda url: "@lmstudio (LM Studio)\n\nDive into the docs https://lmstudio.ai/docs/integrations/hermes",
    )
    monkeypatch.setattr(xst, "_scrape_via_jina", lambda url: "Dive into the docs")

    result = xst._scrape_one("https://x.com/lmstudio/status/2049878775039406221")

    assert result["source"] == "fxtwitter"
    assert "lmstudio.ai/docs/integrations/hermes" in result["content"]


def test_x_scrape_tool_batches_in_parallel_and_preserves_order(monkeypatch):
    urls = [
        "https://x.com/a/status/100",
        "https://x.com/b/status/200",
        "https://x.com/c/status/300",
    ]

    monkeypatch.setattr(xst, "_scrape_via_apify", lambda batch: None)
    monkeypatch.setattr(
        xst,
        "_scrape_one",
        lambda url: {"url": url, "source": "unit", "content": f"content for {url}"},
    )

    payload = json.loads(xst.x_scrape_tool(urls))

    assert payload["count"] == 3
    assert payload["source"] == "unit"
    assert [item["url"] for item in payload["results"]] == urls
    assert "Source URL: https://x.com/a/status/100" in payload["content"]


def test_gateway_x_context_prompt_handles_many_links(monkeypatch):
    import gateway.run as gateway_run

    seen = []

    def fake_scrape(urls):
        seen.append(list(urls))
        return json.dumps({"content": "batched tweet content", "source": "unit"})

    monkeypatch.setattr("tools.x_scraper_tool.x_scrape_tool", fake_scrape)

    message = " ".join(f"https://x.com/u/status/{100 + i}" for i in range(11))
    prompt = gateway_run._build_x_link_context_prompt(message)

    assert len(seen[0]) == 11
    assert "batched tweet content" in prompt
    assert "https://x.com/u/status/110" in prompt
