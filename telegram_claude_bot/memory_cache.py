"""
Memory and session caching system to avoid repeated filesystem reads.
Implements the PERSONA_CACHE pattern with write batching.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class MemoryCache:
    """
    Cache for SOUL, USER, and MEMORY files.
    Loads once on startup, keeps in RAM.
    """

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        # Cached content
        self.memory_md: str = ""
        self.soul_md: str = ""
        self.user_md: str = ""

        # Paths
        self.memory_file = memory_dir / "MEMORY.md"
        self.soul_file = memory_dir / "SOUL.md"
        self.user_file = memory_dir / "USER.md"

    async def load(self) -> None:
        """Load all memory files into cache."""
        self.memory_md = await self._load_file(self.memory_file)
        self.soul_md = await self._load_file(self.soul_file)
        self.user_md = await self._load_file(self.user_file)
        logger.info(f"Loaded memory cache: MEMORY={len(self.memory_md)} chars, SOUL={len(self.soul_md)} chars, USER={len(self.user_md)} chars")

    async def reload(self) -> None:
        """Reload all memory files from disk."""
        await self.load()
        logger.info("Memory cache reloaded from disk")

    async def _load_file(self, path: Path) -> str:
        """Load a file, return empty string if not exists."""
        if path.exists():
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        return ""

    async def append_to_memory(self, text: str) -> None:
        """Append text to MEMORY.md (both cache and disk)."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_line = f"\n- [{timestamp}] {text}\n"

        # Update cache
        self.memory_md = self.memory_md.rstrip() + new_line

        # Write to disk atomically
        tmp = self.memory_file.with_suffix(".tmp")
        await asyncio.to_thread(tmp.write_text, self.memory_md, encoding="utf-8")
        await asyncio.to_thread(tmp.rename, self.memory_file)

        logger.info(f"Appended to MEMORY.md: {text[:50]}...")

    def get_memory(self) -> str:
        """Get cached MEMORY.md content."""
        return self.memory_md

    def get_soul(self) -> str:
        """Get cached SOUL.md content."""
        return self.soul_md

    def get_user(self) -> str:
        """Get cached USER.md content."""
        return self.user_md


class SessionCache:
    """
    Cache for session JSON files with write batching.
    Keeps sessions in RAM, flushes dirty ones periodically.
    """

    def __init__(self, data_dir: Path, flush_interval: float = 5.0):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.flush_interval = flush_interval

        # session_key -> session_data
        self._cache: Dict[str, dict] = {}

        # session_key -> is_dirty
        self._dirty: Dict[str, bool] = {}

        # Background flush task
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        """Start background flush task."""
        if not self._running:
            self._running = True
            self._flush_task = asyncio.create_task(self._flush_loop())
            logger.info("Session cache flush task started")

    def stop(self) -> None:
        """Stop background flush task and flush all dirty sessions."""
        if self._running:
            self._running = False
            if self._flush_task:
                self._flush_task.cancel()
            logger.info("Session cache flush task stopped")

    async def _flush_loop(self) -> None:
        """Background task that flushes dirty sessions periodically."""
        try:
            while self._running:
                await asyncio.sleep(self.flush_interval)
                await self._flush_dirty_sessions()
        except asyncio.CancelledError:
            # Final flush on cancellation
            await self._flush_dirty_sessions()
            logger.info("Session cache final flush complete")

    async def _flush_dirty_sessions(self) -> None:
        """Write all dirty sessions to disk."""
        if not self._dirty:
            return

        dirty_keys = [k for k, is_dirty in self._dirty.items() if is_dirty]

        for key in dirty_keys:
            if key in self._cache:
                await self._write_session_to_disk(key, self._cache[key])
                self._dirty[key] = False

        if dirty_keys:
            logger.info(f"Flushed {len(dirty_keys)} dirty session(s) to disk")

    async def _write_session_to_disk(self, key: str, data: dict) -> None:
        """Write session data to disk atomically."""
        file_path = self.data_dir / f"{key}.json"
        tmp_path = file_path.with_suffix(".tmp")

        json_str = json.dumps(data, indent=2)
        await asyncio.to_thread(tmp_path.write_text, json_str, encoding="utf-8")
        await asyncio.to_thread(tmp_path.rename, file_path)

    async def get_session(self, key: str) -> Optional[dict]:
        """Get session from cache, load from disk if needed."""
        if key in self._cache:
            return self._cache[key]

        # Load from disk
        file_path = self.data_dir / f"{key}.json"
        if file_path.exists():
            try:
                content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
                data = json.loads(content)
                self._cache[key] = data
                self._dirty[key] = False
                return data
            except Exception as e:
                logger.error(f"Error loading session {key}: {e}")
                return None

        return None

    def set_session(self, key: str, data: dict) -> None:
        """Update session in cache and mark as dirty."""
        self._cache[key] = data
        self._dirty[key] = True

    def delete_session(self, key: str) -> None:
        """Delete session from cache (and eventually disk)."""
        if key in self._cache:
            del self._cache[key]
        if key in self._dirty:
            del self._dirty[key]

        # Delete from disk immediately
        file_path = self.data_dir / f"{key}.json"
        if file_path.exists():
            file_path.unlink()

    def clear_all(self) -> None:
        """Clear all cached sessions."""
        self._cache.clear()
        self._dirty.clear()


class DailyScratchpad:
    """
    Append-only daily log files: memory/2026-03-07.md
    """

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    async def append(self, text: str) -> None:
        """Append text to today's scratchpad."""
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = self.memory_dir / f"{today}.md"

        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {text}\n"

        # Append to file
        await asyncio.to_thread(self._append_to_file, file_path, entry)
        logger.debug(f"Appended to scratchpad {today}.md")

    def _append_to_file(self, path: Path, content: str) -> None:
        """Append content to file (thread-safe)."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)

    async def get_today(self) -> str:
        """Get today's scratchpad content."""
        today = datetime.now().strftime("%Y-%m-%d")
        file_path = self.memory_dir / f"{today}.md"

        if file_path.exists():
            return await asyncio.to_thread(file_path.read_text, encoding="utf-8")
        return ""
