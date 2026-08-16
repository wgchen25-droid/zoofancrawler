"""Six-hour scheduler for the configured crawler."""

from __future__ import annotations

import logging
from typing import Any, Optional


LOGGER = logging.getLogger(__name__)


def _positive_limit(value: Optional[int]) -> Optional[int]:
    """Validate an optional scheduler candidate limit."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_candidates_per_source must be a positive integer")
    return value


class CrawlScheduler:
    """Schedule ``Crawler.crawl('all')`` without inventing sources.

    APScheduler is used when installed (it is an optional runtime dependency),
    while ``run_once`` remains useful in tests and constrained deployments.
    """

    def __init__(
        self,
        crawler: Any,
        *,
        hours: float = 6.0,
        max_candidates_per_source: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.crawler = crawler
        self.hours = float(hours)
        self.max_candidates_per_source = _positive_limit(max_candidates_per_source)
        self.logger = logger or LOGGER
        self._scheduler: Any = None

    def run_once(self) -> Any:
        """Run only configured enabled sources through the crawler."""

        if self.max_candidates_per_source is None:
            # Omitting the keyword is intentional: the Crawler owns the
            # default candidate limit for scheduled runs.
            return self.crawler.crawl("all")
        return self.crawler.crawl(
            "all",
            max_candidates_per_source=self.max_candidates_per_source,
        )

    def start(self, *, blocking: bool = True) -> Any:
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            # A deterministic fallback is preferable to silently crawling an
            # unconfigured endpoint.  Callers can use run_once explicitly.
            self.logger.warning("APScheduler is unavailable; scheduler not started")
            return self.run_once()
        scheduler = BlockingScheduler()
        scheduler.add_job(self.run_once, "interval", hours=self.hours, id="zoofan-crawl", replace_existing=True)
        self._scheduler = scheduler
        if blocking:
            scheduler.start()
        return scheduler

    def stop(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)


def schedule(
    crawler: Any,
    *,
    hours: float = 6.0,
    max_candidates_per_source: Optional[int] = None,
    blocking: bool = True,
) -> Any:
    """Convenience wrapper around :class:`CrawlScheduler`."""

    scheduler_kwargs: dict[str, Any] = {"hours": hours}
    if max_candidates_per_source is not None:
        scheduler_kwargs["max_candidates_per_source"] = max_candidates_per_source
    return CrawlScheduler(crawler, **scheduler_kwargs).start(blocking=blocking)


__all__ = ["CrawlScheduler", "schedule"]
