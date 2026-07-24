"""Web search tool — DuckDuckGo backend (no API key).

Provides Researcher with live web search for competitive intelligence, market
research, and citation-backed findings. Uses DuckDuckGo via the ``ddgs``
package, which needs NO API key or account — so the tool works out of the box
in every deployment (no SSM secret to provision, no runtime-role grant).

If a search fails (rate limit, network), the tool returns a structured error
the agent can report honestly instead of raising.
"""

from strands import tool


@tool
def web_search(
    query: str,
    max_results: int = 5,
    region: str = "us-en",
) -> dict:
    """Search the web for current information with source URLs.

    Use this for competitive intelligence, market research, pricing checks,
    product launch tracking, and any claim that needs a citable source.
    Every result includes a URL — use it in the Sources section of your reply.

    Args:
        query: Search query. Be specific. Prefer "Competitor X pricing 2026"
            over "pricing info".
        max_results: How many results to return. Default 5. Max 10.
        region: DuckDuckGo region code (e.g. "us-en", "uk-en", "de-de").

    Returns:
        Dict with:
        - results: List of {title, url, content}
        - query: The query that was run
        - error: Present only if the search failed (report it, don't invent
          results)
    """
    from ddgs import DDGS

    capped = min(max(max_results, 1), 10)
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, region=region, max_results=capped))
    except Exception as exc:  # noqa: BLE001 — surface, never crash the run
        return {"query": query, "results": [], "error": f"search failed: {exc}"}

    return {
        "query": query,
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("href", "") or r.get("url", ""),
                "content": r.get("body", ""),
            }
            for r in raw
        ],
    }
