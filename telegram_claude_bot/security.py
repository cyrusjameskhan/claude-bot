"""
Security module for Telegram Claude Bot.
Handles user authentication, pairing, and command validation.
"""

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Optional, Set, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from config import ALLOWED_TASK_TYPES, ALLOWED_SHELL_COMMANDS, FORBIDDEN_PATTERNS


@dataclass
class PairedUser:
    """Represents a paired/authenticated user."""
    user_id: int
    username: Optional[str]
    paired_at: str
    last_active: str
    session_token: str
    
    
class SecurityManager:
    """Manages user authentication and session security."""
    
    def __init__(self, allowed_user_ids: list[int], pairing_secret: str, data_dir: Path = None):
        self.allowed_user_ids: Set[int] = set(allowed_user_ids)
        self.pairing_secret = pairing_secret
        self.data_dir = data_dir or Path("data")
        self.data_dir.mkdir(exist_ok=True)
        
        self._paired_users_file = self.data_dir / "paired_users.json"
        self._pending_pairings: dict[int, Tuple[str, float]] = {}  # user_id -> (code, timestamp)
        self._paired_users: dict[int, PairedUser] = {}
        
        self._load_paired_users()
    
    def _load_paired_users(self) -> None:
        """Load paired users from persistent storage."""
        if self._paired_users_file.exists():
            try:
                data = json.loads(self._paired_users_file.read_text())
                self._paired_users = {
                    int(uid): PairedUser(**user_data) 
                    for uid, user_data in data.items()
                }
            except (json.JSONDecodeError, KeyError):
                self._paired_users = {}
    
    def _save_paired_users(self) -> None:
        """Save paired users to persistent storage."""
        data = {str(uid): asdict(user) for uid, user in self._paired_users.items()}
        self._paired_users_file.write_text(json.dumps(data, indent=2))
    
    def is_user_allowed(self, user_id: int) -> bool:
        """Check if user ID is in the allowlist."""
        return user_id in self.allowed_user_ids
    
    def is_user_paired(self, user_id: int) -> bool:
        """Check if user has completed pairing."""
        return user_id in self._paired_users
    
    def generate_pairing_code(self, user_id: int) -> Optional[str]:
        """Generate a one-time pairing code for a user."""
        if not self.is_user_allowed(user_id):
            return None
        
        # Generate a 6-digit code
        code = f"{secrets.randbelow(1000000):06d}"
        self._pending_pairings[user_id] = (code, time.time())
        return code
    
    def verify_pairing(self, user_id: int, secret: str, username: Optional[str] = None) -> Tuple[bool, str]:
        """
        Verify pairing attempt with the shared secret.
        Returns (success, message).
        """
        if not self.is_user_allowed(user_id):
            return False, "❌ Your user ID is not in the allowlist."
        
        if self.is_user_paired(user_id):
            return True, "✅ You're already paired!"
        
        # Check if secret matches
        if not secrets.compare_digest(secret.strip(), self.pairing_secret):
            return False, "❌ Invalid pairing secret. Please try again."
        
        # Create session
        session_token = secrets.token_urlsafe(32)
        now = datetime.now().isoformat()
        
        self._paired_users[user_id] = PairedUser(
            user_id=user_id,
            username=username,
            paired_at=now,
            last_active=now,
            session_token=session_token
        )
        self._save_paired_users()
        
        # Clean up pending pairing
        self._pending_pairings.pop(user_id, None)
        
        return True, "✅ Pairing successful! You can now use the bot."
    
    def update_activity(self, user_id: int) -> None:
        """Update last active timestamp for a user."""
        if user_id in self._paired_users:
            self._paired_users[user_id].last_active = datetime.now().isoformat()
            self._save_paired_users()
    
    def revoke_user(self, user_id: int) -> bool:
        """Revoke a user's pairing."""
        if user_id in self._paired_users:
            del self._paired_users[user_id]
            self._save_paired_users()
            return True
        return False
    
    def get_user_info(self, user_id: int) -> Optional[PairedUser]:
        """Get information about a paired user."""
        return self._paired_users.get(user_id)


class CommandValidator:
    """Validates and sanitizes commands before execution."""
    
    @staticmethod
    def validate_task_type(task_type: str) -> Tuple[bool, str]:
        """Check if task type is allowed."""
        task_type = task_type.lower().strip()
        if task_type in ALLOWED_TASK_TYPES:
            return True, ALLOWED_TASK_TYPES[task_type]
        return False, f"Unknown task type. Allowed: {', '.join(ALLOWED_TASK_TYPES.keys())}"
    
    @staticmethod
    def check_forbidden_patterns(text: str) -> Tuple[bool, Optional[str]]:
        """
        Check if text contains forbidden patterns.
        Returns (is_safe, matched_pattern).
        """
        text_lower = text.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.lower() in text_lower:
                return False, pattern
        return True, None
    
    @staticmethod
    def validate_shell_command(command: str) -> Tuple[bool, str]:
        """
        Validate a shell command against the allowlist.
        Returns (is_allowed, reason).
        """
        command = command.strip()
        
        # Check forbidden patterns first
        is_safe, pattern = CommandValidator.check_forbidden_patterns(command)
        if not is_safe:
            return False, f"Command contains forbidden pattern: {pattern}"
        
        # Check if command starts with an allowed command
        for allowed in ALLOWED_SHELL_COMMANDS:
            if command == allowed or command.startswith(allowed + " "):
                return True, "Command allowed"
        
        return False, f"Command not in allowlist. Allowed commands: {', '.join(sorted(ALLOWED_SHELL_COMMANDS))}"
    
    @staticmethod
    def sanitize_input(text: str, max_length: int = 4000) -> str:
        """Sanitize user input."""
        # Truncate if too long
        if len(text) > max_length:
            text = text[:max_length] + "... (truncated)"
        
        # Remove null bytes
        text = text.replace("\x00", "")
        
        return text.strip()
    
    @staticmethod
    def parse_command(message: str) -> Tuple[Optional[str], str]:
        """
        Parse a message into task type and content.
        Format: /task_type content
        Or: task_type: content
        Returns (task_type, content).
        """
        message = message.strip()
        
        # Check for /command format
        if message.startswith("/"):
            parts = message[1:].split(None, 1)
            if parts:
                task_type = parts[0].lower()
                content = parts[1] if len(parts) > 1 else ""
                return task_type, content
        
        # Check for "task_type: content" format
        if ":" in message:
            parts = message.split(":", 1)
            potential_type = parts[0].strip().lower()
            if potential_type in ALLOWED_TASK_TYPES:
                return potential_type, parts[1].strip()
        
        # Default to general task
        return None, message


def create_security_manager(settings) -> SecurityManager:
    """Factory function to create SecurityManager from settings."""
    return SecurityManager(
        allowed_user_ids=settings.allowed_user_ids_list,
        pairing_secret=settings.pairing_secret
    )
