"""Typed subset of APScheduler's blocking scheduler API."""

from typing import Callable, Mapping, Optional

from apscheduler.job import Job


class BlockingScheduler:
    def __init__(self, gconfig: Optional[Mapping[str, object]] = ..., **options: object) -> None: ...

    @property
    def running(self) -> bool: ...

    def add_job(
        self,
        func: Callable[..., object],
        trigger: str = ...,
        *,
        id: Optional[str] = ...,
        replace_existing: bool = ...,
        hours: float = ...,
        **trigger_args: object,
    ) -> Job: ...

    def start(self, pause: bool = ...) -> None: ...

    def shutdown(self, wait: bool = ...) -> None: ...
