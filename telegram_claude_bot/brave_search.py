"""
Brave Search API integration for Telegram bot.
Provides web search capabilities using Brave Search API.
"""

import httpx
import logging
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class BraveSearchClient:
    """Client for Brave Search API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.search.brave.com/res/v1"

    async def web_search(
        self,
        query: str,
        count: int = 5,
        country: str = "US"
    ) -> Dict:
        """
        Perform a web search using Brave Search API.

        Args:
            query: Search query string
            count: Number of results to return (max 20)
            country: Country code for localized results

        Returns:
            Dict with search results
        """
        url = f"{self.base_url}/web/search"

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key
        }

        params = {
            "q": query,
            "count": min(count, 20),
            "country": country,
            "search_lang": "en",
            "safesearch": "moderate"
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Brave Search API error: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Brave Search error: {e}")
            raise

    def format_results_for_telegram(self, data: Dict, max_results: int = 5) -> str:
        """Format search results for Telegram message."""
        lines = []

        # Get web results
        web_results = data.get("web", {}).get("results", [])

        if not web_results:
            return "No results found."

        lines.append("🔍 **Search Results:**\n")

        for i, result in enumerate(web_results[:max_results], 1):
            title = result.get("title", "No title")
            url = result.get("url", "")
            description = result.get("description", "")

            # Truncate description if too long
            if len(description) > 150:
                description = description[:150] + "..."

            lines.append(f"**{i}. [{title}]({url})**")
            if description:
                lines.append(f"_{description}_\n")

        return "\n".join(lines)


def create_brave_search_client(api_key: str) -> BraveSearchClient:
    """Factory function to create BraveSearchClient."""
    return BraveSearchClient(api_key)
