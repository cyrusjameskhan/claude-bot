"""
Reminder scheduler using APScheduler with SQLite persistence.
Allows AI to schedule one-time and recurring reminders.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, Awaitable
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.job import Job

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """
    Manages scheduled reminders with persistence.
    Supports one-time delays, specific datetimes, and recurring CRON schedules.
    """
    
    def __init__(
        self,
        send_callback: Callable[[int, str], Awaitable[None]],
        db_path: str = "data/reminders.db"
    ):
        """
        Initialize the scheduler.
        
        Args:
            send_callback: Async function to send messages. Takes (chat_id, message).
            db_path: Path to SQLite database for job persistence.
        """
        self.send_callback = send_callback
        self.db_path = Path(db_path)
        
        # Ensure data directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Configure job store for persistence
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{self.db_path}')
        }
        
        # Create scheduler with persistence
        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            job_defaults={
                'coalesce': True,  # Combine missed runs into one
                'max_instances': 1,
                'misfire_grace_time': 3600  # 1 hour grace period for missed jobs
            }
        )
        
        self._started = False
    
    def start(self) -> None:
        """Start the scheduler."""
        if not self._started:
            self.scheduler.start()
            self._started = True
            logger.info(f"Reminder scheduler started. DB: {self.db_path}")
            
            # Log existing jobs
            jobs = self.scheduler.get_jobs()
            if jobs:
                logger.info(f"Loaded {len(jobs)} persisted reminders")
    
    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the scheduler gracefully."""
        if self._started:
            self.scheduler.shutdown(wait=wait)
            self._started = False
            logger.info("Reminder scheduler stopped")
    
    async def _send_reminder(self, chat_id: int, message: str, job_id: str) -> None:
        """
        Internal callback that fires when a reminder triggers.
        """
        try:
            formatted_message = f"⏰ **Reminder**\n\n{message}"
            await self.send_callback(chat_id, formatted_message)
            logger.info(f"Sent reminder {job_id} to chat {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send reminder {job_id}: {e}")
    
    def _generate_job_id(self, chat_id: int) -> str:
        """Generate a unique job ID."""
        return f"reminder_{chat_id}_{uuid4().hex[:8]}"
    
    def add_delay_reminder(
        self,
        chat_id: int,
        message: str,
        minutes: int = 0,
        hours: int = 0,
        days: int = 0
    ) -> tuple[str, datetime]:
        """
        Add a one-time reminder after a delay.
        
        Args:
            chat_id: Telegram chat ID to send reminder to.
            message: Reminder message text.
            minutes: Minutes from now.
            hours: Hours from now.
            days: Days from now.
            
        Returns:
            Tuple of (job_id, scheduled_time)
        """
        total_seconds = (days * 86400) + (hours * 3600) + (minutes * 60)
        if total_seconds <= 0:
            raise ValueError("Delay must be positive")
        
        run_time = datetime.now() + timedelta(seconds=total_seconds)
        job_id = self._generate_job_id(chat_id)
        
        self.scheduler.add_job(
            self._send_reminder,
            trigger=DateTrigger(run_date=run_time),
            args=[chat_id, message, job_id],
            id=job_id,
            name=f"Reminder: {message[:50]}",
            replace_existing=True
        )
        
        logger.info(f"Scheduled delay reminder {job_id} for {run_time}")
        return job_id, run_time
    
    def add_datetime_reminder(
        self,
        chat_id: int,
        message: str,
        run_at: datetime
    ) -> tuple[str, datetime]:
        """
        Add a one-time reminder at a specific datetime.
        
        Args:
            chat_id: Telegram chat ID to send reminder to.
            message: Reminder message text.
            run_at: Datetime to send the reminder.
            
        Returns:
            Tuple of (job_id, scheduled_time)
        """
        if run_at <= datetime.now():
            raise ValueError("Scheduled time must be in the future")
        
        job_id = self._generate_job_id(chat_id)
        
        self.scheduler.add_job(
            self._send_reminder,
            trigger=DateTrigger(run_date=run_at),
            args=[chat_id, message, job_id],
            id=job_id,
            name=f"Reminder: {message[:50]}",
            replace_existing=True
        )
        
        logger.info(f"Scheduled datetime reminder {job_id} for {run_at}")
        return job_id, run_at
    
    def add_cron_reminder(
        self,
        chat_id: int,
        message: str,
        cron_expression: Optional[str] = None,
        # Individual cron fields (alternative to expression)
        minute: str = "*",
        hour: str = "*",
        day: str = "*",
        month: str = "*",
        day_of_week: str = "*"
    ) -> tuple[str, str]:
        """
        Add a recurring reminder using CRON schedule.
        
        Args:
            chat_id: Telegram chat ID to send reminder to.
            message: Reminder message text.
            cron_expression: Standard cron expression (e.g., "0 9 * * MON")
            minute, hour, day, month, day_of_week: Individual cron fields.
            
        Returns:
            Tuple of (job_id, cron_description)
        """
        job_id = self._generate_job_id(chat_id)
        
        if cron_expression:
            # Parse standard cron expression: "minute hour day month day_of_week"
            parts = cron_expression.strip().split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
            else:
                raise ValueError(f"Invalid cron expression: {cron_expression}")
        
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week
        )
        
        self.scheduler.add_job(
            self._send_reminder,
            trigger=trigger,
            args=[chat_id, message, job_id],
            id=job_id,
            name=f"Recurring: {message[:50]}",
            replace_existing=True
        )
        
        cron_desc = f"{minute} {hour} {day} {month} {day_of_week}"
        logger.info(f"Scheduled cron reminder {job_id}: {cron_desc}")
        return job_id, cron_desc
    
    def cancel_reminder(self, job_id: str) -> bool:
        """
        Cancel a scheduled reminder.
        
        Args:
            job_id: The reminder's job ID.
            
        Returns:
            True if cancelled, False if not found.
        """
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Cancelled reminder {job_id}")
            return True
        except Exception:
            return False
    
    def get_reminders(self, chat_id: Optional[int] = None) -> list[dict]:
        """
        Get all scheduled reminders, optionally filtered by chat_id.
        
        Args:
            chat_id: Optional chat ID to filter by.
            
        Returns:
            List of reminder dictionaries.
        """
        jobs = self.scheduler.get_jobs()
        reminders = []
        
        for job in jobs:
            # Extract chat_id from job args
            job_chat_id = job.args[0] if job.args else None
            
            if chat_id is not None and job_chat_id != chat_id:
                continue
            
            reminder = {
                'id': job.id,
                'name': job.name,
                'message': job.args[1] if len(job.args) > 1 else "",
                'chat_id': job_chat_id,
                'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
                'trigger_type': type(job.trigger).__name__
            }
            reminders.append(reminder)
        
        return reminders
    
    def get_reminder_count(self, chat_id: Optional[int] = None) -> int:
        """Get count of scheduled reminders."""
        return len(self.get_reminders(chat_id))


# Global scheduler instance (set by bot.py on startup)
_scheduler: Optional[ReminderScheduler] = None


def get_scheduler() -> Optional[ReminderScheduler]:
    """Get the global scheduler instance."""
    return _scheduler


def set_scheduler(scheduler: ReminderScheduler) -> None:
    """Set the global scheduler instance."""
    global _scheduler
    _scheduler = scheduler
