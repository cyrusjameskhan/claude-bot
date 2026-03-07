"""
Configuration management for the Telegram Claude Bot.
Loads settings from environment variables with validation.
"""

from pathlib import Path
from typing import List, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable loading."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # Telegram
    telegram_bot_token: str
    allowed_user_ids: str  # Comma-separated list
    
    # Security
    pairing_secret: str
    encryption_key: Optional[str] = None
    
    # Server
    use_webhook: bool = False
    webhook_url: Optional[str] = None
    server_port: int = 8443
    
    # Claude Code
    claude_code_path: Optional[str] = None
    claude_working_dir: Optional[str] = None
    task_timeout: int = 300
    claude_model: str = "claude-sonnet-4-5"  # Default model
    
    # Whisper
    whisper_model: str = "base"
    whisper_device: str = "cpu"

    # Brave Search
    brave_search_api_key: Optional[str] = None
    
    @property
    def allowed_user_ids_list(self) -> List[int]:
        """Parse comma-separated user IDs into a list of integers."""
        if not self.allowed_user_ids:
            return []
        return [int(uid.strip()) for uid in self.allowed_user_ids.split(",") if uid.strip()]
    
    @property
    def working_directory(self) -> Path:
        """Get the working directory for Claude Code tasks."""
        if self.claude_working_dir:
            return Path(self.claude_working_dir)
        # Default to user's home directory for broader access
        return Path.home()
    
    @field_validator("pairing_secret")
    @classmethod
    def validate_pairing_secret(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Pairing secret must be at least 8 characters")
        if v == "change_this_to_a_strong_secret":
            raise ValueError("Please change the default pairing secret!")
        return v


# Allowed commands/task types that Claude Code can execute
ALLOWED_TASK_TYPES = {
    "code": "Write or modify code",
    "explain": "Explain code or concepts",
    "review": "Review code for issues",
    "debug": "Help debug an issue",
    "test": "Write or run tests",
    "docs": "Generate documentation",
    "refactor": "Refactor existing code",
    "shell": "Execute approved shell commands",
}

# Shell commands that are explicitly allowed (for 'shell' task type)
ALLOWED_SHELL_COMMANDS = {
    "git status",
    "git log --oneline -10",
    "git diff",
    "git branch",
    "ls",
    "dir",
    "pwd",
    "cat",
    "type",  # Windows equivalent of cat
    "pip list",
    "python --version",
    "node --version",
    "npm list --depth=0",
}

# Patterns that are NEVER allowed in any command
FORBIDDEN_PATTERNS = [
    "rm -rf",
    "del /f /s /q",
    "format c:",
    "format /dev/",
    "mkfs",
    ":(){:|:&};:",  # Fork bomb
    "dd if=",
    "> /dev/",
    "chmod 777",
    "curl | sh",
    "wget | sh",
    "eval(",
    "exec(",
    "sudo",
    "su -",
    "passwd",
    "shutdown",
    "reboot",
    "registry",
    "regedit",
]


def load_settings() -> Settings:
    """Load and validate settings from environment."""
    return Settings()
