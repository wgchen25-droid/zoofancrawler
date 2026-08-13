"""Six-hour scheduler for the configured crawler."""

from __future__ import annotations

import logging
from typing import Any, Optional


LOGGER = logging.getLogger(__name__)


class CrawlScheduler:
    """Schedule ``Crawler.crawl('all')`` without inventing sources.

    APScheduler is used when installed (it is an optional runtime dependency),
    while ``run_once`` remains useful in tests and constrained deployments.
    """

    def __init__(self, crawler: Any, *, hours: float = 6.0, logger: Optional[logging.Logger] = None) -> None:
        self.crawler = crawler
        self.hours = float(hours)
        self.logger = logger or LOGGER
        self._scheduler: Any = None

    def run_once(self) -> Any:
        """Run only configured enabled sources through the crawler."""

        return self.crawler.crawl("all")

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


def schedule(crawler: Any, *, hours: float = 6.0, blocking: bool = True) -> Any:
    """Convenience wrapper around :class:`CrawlScheduler`."""

    return CrawlScheduler(crawler, hours=hours).start(blocking=blocking)


__all__ = ["CrawlScheduler", "schedule"]
