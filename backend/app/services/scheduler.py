from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.services.settings import get_settings_map


class SchedulerService:
    def __init__(self, auto_mode_service, session_factory) -> None:
        self.auto_mode_service = auto_mode_service
        self.session_factory = session_factory
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    async def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        await self.reload(start_auto_mode_now=True)

    def auto_mode_next_run_at(self):
        job = self.scheduler.get_job("auto-mode-scan")
        return None if job is None else job.next_run_time

    async def reload(self, *, start_auto_mode_now: bool = False) -> None:
        if self.scheduler.get_job("auto-mode-scan"):
            self.scheduler.remove_job("auto-mode-scan")

        async with self.session_factory() as session:
            settings_map = await get_settings_map(session)

        if settings_map.get("auto_mode_enabled", "false").lower() != "true":
            return
        if settings_map.get("auto_mode_paused", "false").lower() == "true":
            return

        self.scheduler.add_job(
            self._run_auto_mode_cycle,
            CronTrigger(minute="*/15", second=5),
            id="auto-mode-scan",
            replace_existing=True,
        )
        if start_auto_mode_now:
            await self.auto_mode_service.queue_cycle(reason="enabled")

    async def _run_auto_mode_cycle(self) -> None:
        await self.auto_mode_service.run_cycle(reason="15m_close")

    async def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
