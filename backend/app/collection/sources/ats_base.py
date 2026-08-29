"""Shared behaviour for public ATS job-board feeds.

These are the depth sources.  Each returns one company's entire published board
over an unauthenticated public JSON endpoint -- free, unlimited, and crucially
carrying the *complete* description text that skills extraction, description
Boolean filters and sponsorship detection all depend on.

The trade-off is that they answer "what is this company hiring for?" and not
"who is hiring for this role?".  That is what the ATS discovery loop in
`ats_discovery.py` solves: an aggregator call finds the company once, and this
layer then monitors it indefinitely at zero cost.
"""
from __future__ import annotations

import logging

from ..base import (BaseSource, Budget, CollectionQuery, RawPosting,
                    SourceCapabilities)

log = logging.getLogger(__name__)


class AtsSource(BaseSource):
    kind = "ats"
    capabilities = SourceCapabilities(
        keyword_search=False,
        full_description=True,
        requires_company_token=True,
        countries=("US", "GLOBAL"),
    )

    def board_url(self, token: str) -> str:
        raise NotImplementedError

    def parse_board(self, data, token: str, company_name: str | None
                    ) -> list[RawPosting]:
        raise NotImplementedError

    async def search(self, query: CollectionQuery,
                     budget: Budget) -> list[RawPosting]:
        tokens = query.company_tokens or []
        if not tokens:
            log.debug("[%s] no company tokens for this run", self.key)
            return []

        postings: list[RawPosting] = []
        for token, company_name in tokens:
            if not budget.can_call():
                log.info("[%s] budget exhausted after %d companies",
                         self.key, len(postings))
                break
            budget.spend_call()
            try:
                data = await self.client.get_json(
                    self.board_url(token), source_key=self.key)
            except Exception as exc:                  # noqa: BLE001
                log.warning("[%s] board '%s' failed: %s", self.key, token, exc)
                continue
            if not data:
                continue
            batch = self.parse_board(data, token, company_name)
            postings.extend(batch[:budget.remaining_results])
            budget.add_results(len(batch))

        log.info("[%s] %d postings from %d board(s)", self.key,
                 len(postings), len(tokens))
        return postings

    # -- local keyword narrowing ----------------------------------------
    @staticmethod
    def matches_query(posting: RawPosting, query: CollectionQuery) -> bool:
        """ATS boards cannot filter server-side, so narrow locally."""
        needle = query.query_text().lower().strip()
        if not needle:
            return True
        haystack = " ".join(filter(None, [
            posting.title, posting.description_text,
            posting.description_html, posting.location_raw,
        ])).lower()
        return any(word in haystack for word in needle.split())
