"""
Claude Code subprocess runner with strict task management.
Executes Claude Code CLI as a subprocess with timeout and output handling.
"""

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
import logging

from security import CommandValidator

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Status of a Claude Code task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


@dataclass
class TaskResult:
    """Result of a Claude Code task execution."""
    status: TaskStatus
    output: Optional[str]
    error: Optional[str]
    execution_time: float
    task_type: Optional[str]
    created_at: str
    
    def to_telegram_message(self, max_length: int = 4000) -> str:
        """Format result for Telegram message."""
        status_emoji = {
            TaskStatus.COMPLETED: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.TIMEOUT: "⏰",
            TaskStatus.REJECTED: "🚫",
            TaskStatus.RUNNING: "⏳",
            TaskStatus.PENDING: "📋",
        }
        
        emoji = status_emoji.get(self.status, "❓")
        
        lines = [
            f"{emoji} **Task {self.status.value.upper()}**",
            f"⏱️ Time: {self.execution_time:.1f}s",
        ]
        
        if self.task_type:
            lines.append(f"📂 Type: {self.task_type}")
        
        if self.output:
            output = self.output
            # Reserve space for metadata
            available = max_length - len("\n".join(lines)) - 50
            if len(output) > available:
                output = output[:available] + "\n... (truncated)"
            lines.append(f"\n📤 **Output:**\n```\n{output}\n```")
        
        if self.error:
            error = self.error[:500] if len(self.error) > 500 else self.error
            lines.append(f"\n⚠️ **Error:**\n```\n{error}\n```")
        
        return "\n".join(lines)


class ClaudeCodeRunner:
    """
    Manages Claude Code CLI and Ollama subprocess execution.
    Enforces timeouts, validates commands, and captures output.
    """
    
    # Available models - Claude and Ollama
    AVAILABLE_MODELS = {
        # Claude models
        "opus": "claude-opus-4-20250514",
        "sonnet": "claude-sonnet-4-20250514", 
        "haiku": "claude-haiku-4-20250514",
        # Ollama models (prefix with ollama:)
        # GLM-4.7-flash is RECOMMENDED - properly supports tool calling format
        "glm": "ollama:glm-4.7-flash",
        "glm4": "ollama:glm-4.7-flash",
        "local": "ollama:slekrem/gpt-oss-claude-code-32k:latest",  # Default local model
        "qwen": "ollama:qwen3-coder:30b",
        "qwen3": "ollama:qwen3-coder:30b",
        "qwen-coder": "ollama:qwen3-coder:30b",
        "gemma": "ollama:gemma3:4b",
        "gemma3": "ollama:gemma3:4b",
        "gemma4b": "ollama:gemma3:4b",
        "gpt-oss": "ollama:gpt-oss:20b",
        "gptoss": "ollama:gpt-oss:20b",
        # Claude Code optimized model
        "slekrem": "ollama:slekrem/gpt-oss-claude-code-32k:latest",
        "claude-code": "ollama:slekrem/gpt-oss-claude-code-32k:latest",
        "cc": "ollama:slekrem/gpt-oss-claude-code-32k:latest",
    }
    
    # Ollama API endpoint
    OLLAMA_URL = "http://localhost:11434/api/chat"
    
    def __init__(
        self, 
        working_dir: Path,
        claude_path: Optional[str] = None,
        timeout: int = 300,
        default_model: str = "claude-sonnet-4-20250514"
    ):
        self.working_dir = working_dir
        self.timeout = timeout
        self.default_model = default_model
        
        # Find Claude Code CLI
        self.claude_path = claude_path or self._find_claude_cli()
        if not self.claude_path:
            logger.warning("Claude Code CLI not found in PATH. Will try 'claude' command.")
            self.claude_path = "claude"
        
        self._running_tasks: dict[str, asyncio.subprocess.Process] = {}
        self._user_models: dict[int, str] = {}  # Per-user model preferences
        self._ollama_histories: dict[str, list] = {}  # Conversation history for Ollama
        self.reminder_scheduler = None  # Set by bot.py on startup
    
    def is_ollama_model(self, model: str) -> bool:
        """Check if the model is an Ollama model."""
        return model.startswith("ollama:")
    
    def get_ollama_model_name(self, model: str) -> str:
        """Extract the Ollama model name from the full string."""
        # "ollama:gemma:4b" -> "gemma:4b"
        return model.replace("ollama:", "", 1)
    
    def set_user_model(self, user_id: int, model: str) -> str:
        """Set the model for a user. Returns the full model name."""
        # Allow shorthand names
        if model.lower() in self.AVAILABLE_MODELS:
            model = self.AVAILABLE_MODELS[model.lower()]
        # Allow direct ollama: prefix
        elif model.lower().startswith("ollama:"):
            model = model.lower()
        self._user_models[user_id] = model
        return model
    
    def get_user_model(self, user_id: int) -> str:
        """Get the model for a user, or default."""
        return self._user_models.get(user_id, self.default_model)
    
    def _find_aider_cli(self) -> Optional[str]:
        """Find the aider CLI executable."""
        # Check venv first (where we installed it)
        venv_aider = Path(__file__).parent / "venv" / "Scripts" / "aider.exe"
        if venv_aider.exists():
            return str(venv_aider)
        
        # Fall back to system PATH
        for name in ["aider", "aider.exe"]:
            path = shutil.which(name)
            if path:
                return path
        return None
    
    async def _run_ollama_with_aider(
        self,
        prompt: str,
        model: str,
        user_id: int,
        agent_id: int,
        is_new_session: bool
    ) -> tuple[str, Optional[str]]:
        """
        Run a prompt through aider with Ollama backend.
        This gives full coding capabilities with local models.
        Returns (output, error).
        """
        ollama_model = self.get_ollama_model_name(model)
        
        # Get agent-specific working directory
        agent_workdir = self._get_agent_workdir(user_id, agent_id)
        
        # Find aider
        aider_path = self._find_aider_cli()
        if not aider_path:
            # Fall back to simple Ollama chat if aider not installed
            return await self._run_ollama_chat(prompt, model, user_id, agent_id, is_new_session)
        
        # Build aider command
        # --model: Ollama model
        # --no-git: Don't require git
        # --yes: Auto-confirm
        # --no-show-model-warnings: Suppress env var warnings
        # --no-pretty: Disable fancy output for non-interactive use
        # --message: The prompt
        cmd = [
            aider_path,
            "--model", f"ollama/{ollama_model}",
            "--no-git",
            "--yes",
            "--no-auto-commits",
            "--no-show-model-warnings",
            "--no-pretty",
            "--message", prompt
        ]
        
        # Set environment for Ollama
        env = {**os.environ, "OLLAMA_API_BASE": "http://localhost:11434"}
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(agent_workdir),
                env=env
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout
            )
            
            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()
            
            # Clean up aider's verbose output - extract just the model response
            # Aider outputs a lot of metadata, we want just the assistant's response
            lines = output.split('\n')
            clean_lines = []
            for line in lines:
                # Skip aider metadata lines
                if any(skip in line.lower() for skip in [
                    'aider v', 'model:', 'git repo:', 'repo-map:', 
                    'tokens:', 'http', 'warning', 'detected dumb',
                    'aider respects', 'for more info', 'you can skip',
                    'note:', 'release notes'
                ]):
                    continue
                if line.strip():
                    clean_lines.append(line)
            
            clean_output = '\n'.join(clean_lines).strip()
            
            if process.returncode == 0:
                return clean_output or output, None
            else:
                return clean_output or output, error or f"Exit code: {process.returncode}"
                
        except asyncio.TimeoutError:
            return "", f"Task exceeded timeout of {self.timeout}s"
        except FileNotFoundError:
            return await self._run_ollama_chat(prompt, model, user_id, agent_id, is_new_session)
        except Exception as e:
            return "", f"Aider error: {str(e)}"
    
    def _get_ollama_history_key(self, user_id: int, agent_id: int) -> str:
        """Get the key for storing Ollama conversation history."""
        return f"{user_id}_{agent_id}"
    
    # System prompt for Ollama models - agentic capabilities with tools
    OLLAMA_SYSTEM_PROMPT = """You are an AI coding agent running on a Windows PC. You can execute commands, read files, write files, and schedule reminders.

## YOUR TOOLS

### 1. Execute PowerShell commands:
<execute>
YOUR_COMMAND_HERE
</execute>

### 2. Read a file:
<read_file>path/to/file.txt</read_file>

### 3. Write a file:
<write_file path="path/to/file.txt">
file content here
</write_file>

### 4. Schedule a reminder:
<schedule_reminder>{"message": "Your reminder text", "type": "delay", "minutes": 30}</schedule_reminder>

Reminder types:
- **delay**: Set minutes, hours, or days. Example: {"message": "Check oven", "type": "delay", "minutes": 30}
- **datetime**: Set specific time. Example: {"message": "Meeting", "type": "datetime", "datetime": "2026-02-04T15:00:00"}
- **cron**: Recurring schedule. Example: {"message": "Weekly standup", "type": "cron", "cron_expression": "0 9 * * MON"}

Cron format: "minute hour day month day_of_week" (e.g., "0 9 * * MON" = every Monday 9am)

### 5. Signal task completion:
<done>Brief summary of what was accomplished</done>

## HOW TO WORK

1. **Plan first** - Think about what steps are needed
2. **Execute step by step** - Use tools, wait for results
3. **Adapt based on output** - If something fails, try another approach
4. **Signal completion** - Use <done> when finished

## EXAMPLE: Complex task

User: "Create a Python script that prints hello world and run it"

You respond:
I'll create the script and run it.

<write_file path="hello.py">
print("Hello, World!")
</write_file>

(System shows: File written successfully)

Now I'll run it:

<execute>
python hello.py
</execute>

(System shows: Hello, World!)

<done>Created hello.py and executed it successfully. Output was "Hello, World!"</done>

## EXAMPLE: Setting a reminder

User: "Remind me to check the oven in 30 minutes"

You respond:
I'll set a reminder for you.

<schedule_reminder>{"message": "Check the oven", "type": "delay", "minutes": 30}</schedule_reminder>

<done>Reminder set! I'll notify you in 30 minutes to check the oven.</done>

## RULES
1. Use ONE tool per response, then wait for the result
2. After seeing results, decide next step
3. Always use <done> when the task is complete
4. Be concise - this is a chat interface
5. You can do multiple iterations until the task is done

## SECURITY - NEVER:
- Delete system files
- Modify registry
- Access credentials
- Run network attacks
- Format drives

## WORKING DIRECTORY
You're working in the user's home folder. Use relative paths when possible."""

    async def _execute_powershell(self, command: str, workdir: Path) -> str:
        """Execute a PowerShell command safely and return output."""
        import re
        
        # Security: Block dangerous commands
        dangerous_patterns = [
            r'Remove-Item.*-Recurse.*[/\\](Windows|System32|Program)',
            r'Format-Volume',
            r'Clear-Disk',
            r'Remove-Partition',
            r'Set-ItemProperty.*HKLM',
            r'reg\s+delete',
            r'del\s+/[sf]',
            r'rmdir\s+/s',
            r'format\s+[a-z]:',
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return f"⚠️ BLOCKED: Command matches dangerous pattern"
        
        try:
            process = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-Command", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir)
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30  # 30 second timeout for commands
            )
            
            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()
            
            if error and not output:
                return f"Error: {error}"
            elif error:
                return f"{output}\n\nWarning: {error}"
            else:
                return output or "(no output)"
                
        except asyncio.TimeoutError:
            return "⚠️ Command timed out (30s limit)"
        except Exception as e:
            return f"⚠️ Execution error: {str(e)}"
    
    async def _read_file(self, filepath: str, workdir: Path) -> str:
        """Read a file and return its contents."""
        try:
            # First, try to resolve user paths (Desktop, Documents, etc.)
            resolved = self._resolve_user_path(filepath)
            if resolved:
                full_path = resolved
            elif Path(filepath).is_absolute():
                full_path = Path(filepath)
            else:
                full_path = workdir / filepath
            
            # Security: Don't allow reading system files
            path_str = str(full_path).lower()
            if any(x in path_str for x in ['\\windows\\', '\\system32\\', '\\program files']):
                return "⚠️ BLOCKED: Cannot read system files"
            
            if not full_path.exists():
                return f"Error: File not found: {full_path}"
            
            if full_path.stat().st_size > 100000:  # 100KB limit
                return "Error: File too large (>100KB)"
            
            content = full_path.read_text(encoding='utf-8', errors='replace')
            return content
        except Exception as e:
            return f"Error reading file: {str(e)}"
    
    def _resolve_user_path(self, filepath: str) -> Path:
        """
        Resolve user-friendly paths to actual system paths.
        Handles: Desktop, Documents, Downloads, etc.
        """
        filepath_lower = filepath.lower().replace('\\', '/')
        home = Path.home()
        
        # Map common folder names to actual paths
        user_folders = {
            'desktop': home / 'Desktop',
            '~/desktop': home / 'Desktop',
            'documents': home / 'Documents',
            '~/documents': home / 'Documents',
            'downloads': home / 'Downloads',
            '~/downloads': home / 'Downloads',
            '~': home,
        }
        
        # Check if path starts with a known folder
        for prefix, actual_path in user_folders.items():
            if filepath_lower.startswith(prefix + '/') or filepath_lower == prefix:
                # Replace the prefix with actual path
                remainder = filepath[len(prefix):].lstrip('/\\')
                return actual_path / remainder if remainder else actual_path
        
        # Handle explicit ~ expansion
        if filepath.startswith('~'):
            return home / filepath[1:].lstrip('/\\')
        
        return None  # Not a user path
    
    async def _write_file(self, filepath: str, content: str, workdir: Path) -> str:
        """Write content to a file."""
        try:
            # First, try to resolve user paths (Desktop, Documents, etc.)
            resolved = self._resolve_user_path(filepath)
            if resolved:
                full_path = resolved
            elif Path(filepath).is_absolute():
                full_path = Path(filepath)
            else:
                full_path = workdir / filepath
            
            # Security: Don't allow writing to system locations
            path_str = str(full_path).lower()
            if any(x in path_str for x in ['\\windows\\', '\\system32\\', '\\program files']):
                return "⚠️ BLOCKED: Cannot write to system locations"
            
            # Create parent directories if needed
            full_path.parent.mkdir(parents=True, exist_ok=True)
            
            full_path.write_text(content, encoding='utf-8')
            return f"✅ File written: {full_path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"
    
    async def _schedule_reminder(self, chat_id: int, params: dict) -> str:
        """
        Schedule a reminder using the ReminderScheduler.
        
        Args:
            chat_id: Telegram chat ID to send reminder to.
            params: Dict with message, type, and type-specific parameters.
        """
        if not self.reminder_scheduler:
            return "⚠️ Reminder scheduler not available"
        
        message = params.get("message", "")
        if not message:
            return "⚠️ Reminder message is required"
        
        reminder_type = params.get("type", "delay")
        
        try:
            if reminder_type == "delay":
                minutes = int(params.get("minutes", 0))
                hours = int(params.get("hours", 0))
                days = int(params.get("days", 0))
                
                if minutes == 0 and hours == 0 and days == 0:
                    return "⚠️ Please specify minutes, hours, or days for the delay"
                
                job_id, run_time = self.reminder_scheduler.add_delay_reminder(
                    chat_id=chat_id,
                    message=message,
                    minutes=minutes,
                    hours=hours,
                    days=days
                )
                return f"✅ Reminder scheduled for {run_time.strftime('%Y-%m-%d %H:%M')}\nID: {job_id}"
                
            elif reminder_type == "datetime":
                datetime_str = params.get("datetime", "")
                if not datetime_str:
                    return "⚠️ Please specify a datetime (e.g., 2026-02-04T15:00:00)"
                
                from datetime import datetime as dt
                run_at = dt.fromisoformat(datetime_str)
                
                job_id, run_time = self.reminder_scheduler.add_datetime_reminder(
                    chat_id=chat_id,
                    message=message,
                    run_at=run_at
                )
                return f"✅ Reminder scheduled for {run_time.strftime('%Y-%m-%d %H:%M')}\nID: {job_id}"
                
            elif reminder_type == "cron":
                cron_expression = params.get("cron_expression", "")
                if not cron_expression:
                    return "⚠️ Please specify a cron_expression (e.g., '0 9 * * MON')"
                
                job_id, cron_desc = self.reminder_scheduler.add_cron_reminder(
                    chat_id=chat_id,
                    message=message,
                    cron_expression=cron_expression
                )
                return f"✅ Recurring reminder scheduled: {cron_desc}\nID: {job_id}"
                
            else:
                return f"⚠️ Unknown reminder type: {reminder_type}. Use 'delay', 'datetime', or 'cron'"
                
        except ValueError as e:
            return f"⚠️ Invalid reminder parameters: {str(e)}"
        except Exception as e:
            logger.error(f"Reminder scheduling error: {e}")
            return f"⚠️ Failed to schedule reminder: {str(e)}"
    
    async def _process_tools(self, text: str, workdir: Path, chat_id: int = None) -> tuple[str, list[str], bool]:
        """
        Process all tool tags in the response.
        Returns: (processed_text, list_of_tool_results, is_done)
        """
        import re
        
        tool_results = []
        is_done = False
        result = text
        
        # Check for <done> tag
        done_match = re.search(r'<done>(.*?)</done>', text, re.DOTALL | re.IGNORECASE)
        if done_match:
            is_done = True
            # Keep the done message but mark it
            result = re.sub(r'<done>(.*?)</done>', r'✅ **Done:** \1', result, flags=re.DOTALL | re.IGNORECASE)
        
        # Process <execute> tags
        exec_pattern = r'<execute>\s*(.*?)\s*</execute>'
        for match in re.finditer(exec_pattern, text, re.DOTALL | re.IGNORECASE):
            command = match.group(1).strip()
            output = await self._execute_powershell(command, workdir)
            tool_results.append(f"[EXECUTE] {command}\n[RESULT] {output}")
            # Replace in result
            replacement = f"```powershell\n{command}\n```\n**Output:**\n```\n{output}\n```"
            result = result.replace(match.group(0), replacement, 1)
        
        # Process <read_file> tags
        read_pattern = r'<read_file>\s*(.*?)\s*</read_file>'
        for match in re.finditer(read_pattern, text, re.DOTALL | re.IGNORECASE):
            filepath = match.group(1).strip()
            content = await self._read_file(filepath, workdir)
            tool_results.append(f"[READ_FILE] {filepath}\n[CONTENT]\n{content}")
            # Replace in result
            replacement = f"📄 **Reading {filepath}:**\n```\n{content[:2000]}{'...(truncated)' if len(content) > 2000 else ''}\n```"
            result = result.replace(match.group(0), replacement, 1)
        
        # Process <write_file> tags
        write_pattern = r'<write_file\s+path=["\']([^"\']+)["\']>\s*(.*?)\s*</write_file>'
        for match in re.finditer(write_pattern, text, re.DOTALL | re.IGNORECASE):
            filepath = match.group(1).strip()
            content = match.group(2)
            write_result = await self._write_file(filepath, content, workdir)
            tool_results.append(f"[WRITE_FILE] {filepath}\n[RESULT] {write_result}")
            # Replace in result
            replacement = f"📝 **Writing {filepath}:** {write_result}"
            result = result.replace(match.group(0), replacement, 1)
        
        # Process <schedule_reminder> tags
        reminder_pattern = r'<schedule_reminder>\s*(.*?)\s*</schedule_reminder>'
        for match in re.finditer(reminder_pattern, text, re.DOTALL | re.IGNORECASE):
            params_str = match.group(1).strip()
            try:
                params = json.loads(params_str)
                if chat_id:
                    reminder_result = await self._schedule_reminder(chat_id, params)
                else:
                    reminder_result = "⚠️ Cannot schedule reminder: chat_id not available"
                tool_results.append(f"[SCHEDULE_REMINDER]\n[RESULT] {reminder_result}")
                # Replace in result
                replacement = f"⏰ **Scheduling reminder:** {reminder_result}"
                result = result.replace(match.group(0), replacement, 1)
            except json.JSONDecodeError:
                tool_results.append(f"[SCHEDULE_REMINDER]\n[ERROR] Invalid JSON: {params_str}")
                replacement = f"⚠️ Invalid reminder JSON: {params_str}"
                result = result.replace(match.group(0), replacement, 1)
        
        return result, tool_results, is_done
    
    async def _process_native_tool_calls(
        self, 
        tool_calls: list, 
        thinking: str, 
        workdir: Path,
        chat_id: int = None
    ) -> tuple[str, list[str], bool]:
        """
        Process Ollama's native tool_calls format.
        Returns: (processed_text, list_of_tool_results, is_done)
        """
        tool_results = []
        output_parts = []
        is_done = False
        
        # Include thinking if present
        if thinking:
            output_parts.append(f"💭 *Thinking:* {thinking}")
        
        for call in tool_calls:
            func = call.get("function", {})
            func_name = func.get("name", "")
            args = func.get("arguments", {})
            
            # Handle arguments that might be a string (JSON) or already a dict
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            
            logger.info(f"Executing tool: {func_name} with args: {args}")
            
            if func_name == "write_file":
                filepath = args.get("path", "")
                content = args.get("content", "")
                result = await self._write_file(filepath, content, workdir)
                tool_results.append(f"[WRITE_FILE] {filepath}\n[RESULT] {result}")
                output_parts.append(f"📝 **Writing {filepath}:** {result}")
                
            elif func_name == "read_file":
                filepath = args.get("path", "")
                content = await self._read_file(filepath, workdir)
                tool_results.append(f"[READ_FILE] {filepath}\n[CONTENT]\n{content}")
                preview = content[:500] + "..." if len(content) > 500 else content
                output_parts.append(f"📄 **Reading {filepath}:**\n```\n{preview}\n```")
                
            elif func_name in ["execute", "bash", "shell", "run_command"]:
                command = args.get("command", "") or args.get("cmd", "")
                result = await self._execute_powershell(command, workdir)
                tool_results.append(f"[EXECUTE] {command}\n[RESULT] {result}")
                output_parts.append(f"```powershell\n{command}\n```\n**Output:**\n```\n{result}\n```")
                
            elif func_name == "schedule_reminder":
                if chat_id:
                    result = await self._schedule_reminder(chat_id, args)
                else:
                    result = "⚠️ Cannot schedule reminder: chat_id not available"
                tool_results.append(f"[SCHEDULE_REMINDER]\n[RESULT] {result}")
                output_parts.append(f"⏰ **Scheduling reminder:** {result}")
                
            elif func_name == "done" or func_name == "task_complete":
                is_done = True
                summary = args.get("summary", "") or args.get("message", "Task completed")
                output_parts.append(f"✅ **Done:** {summary}")
                
            else:
                # Unknown tool - log it
                logger.warning(f"Unknown tool call: {func_name}")
                output_parts.append(f"⚠️ Unknown tool: {func_name}")
        
        return "\n\n".join(output_parts), tool_results, is_done
    
    MAX_AGENT_ITERATIONS = 10  # Safety limit for agentic loop
    
    async def _run_ollama_chat(
        self,
        prompt: str,
        model: str,
        user_id: int,
        agent_id: int,
        is_new_session: bool,
        chat_id: int = None
    ) -> tuple[str, Optional[str]]:
        """
        Run a prompt through Ollama API with agentic loop.
        The model can execute multiple steps until it signals <done>.
        Returns (output, error).
        """
        import httpx
        
        ollama_model = self.get_ollama_model_name(model)
        history_key = self._get_ollama_history_key(user_id, agent_id)
        
        # Get agent working directory for command execution
        workdir = self._get_agent_workdir(user_id, agent_id)
        
        # Use user_id as chat_id if not provided (for Telegram, they're typically the same in DMs)
        if chat_id is None:
            chat_id = user_id
        
        # Get or initialize conversation history with system prompt
        if is_new_session or history_key not in self._ollama_histories:
            self._ollama_histories[history_key] = [
                {"role": "system", "content": self.OLLAMA_SYSTEM_PROMPT}
            ]
        
        history = self._ollama_histories[history_key]
        
        # Add user message to history
        history.append({"role": "user", "content": prompt})
        
        all_outputs = []  # Collect all processed outputs for display
        iteration = 0
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                while iteration < self.MAX_AGENT_ITERATIONS:
                    iteration += 1
                    logger.info(f"Ollama agent iteration {iteration} for user {user_id}")
                    
                    # Call Ollama
                    response = await client.post(
                        self.OLLAMA_URL,
                        json={
                            "model": ollama_model,
                            "messages": history,
                            "stream": False,
                            "options": {
                                "num_ctx": 65536  # 64k context - required for proper tool use
                            }
                        }
                    )
                    
                    if response.status_code != 200:
                        return "\n\n---\n\n".join(all_outputs) if all_outputs else "", f"Ollama error: {response.status_code}"
                    
                    data = response.json()
                    message_data = data.get("message", {})
                    assistant_message = message_data.get("content", "")
                    thinking = message_data.get("thinking", "")
                    tool_calls = message_data.get("tool_calls", [])
                    
                    # Log for debugging
                    logger.info(f"Ollama response - content: {len(assistant_message)} chars, thinking: {len(thinking)} chars, tool_calls: {len(tool_calls)}")
                    
                    # Process native Ollama tool_calls if present
                    if tool_calls:
                        processed_message, tool_results, is_done = await self._process_native_tool_calls(
                            tool_calls, thinking, workdir, chat_id
                        )
                    else:
                        # Fall back to XML tag processing for backwards compatibility
                        processed_message, tool_results, is_done = await self._process_tools(assistant_message, workdir, chat_id)
                    
                    # Add to outputs
                    if processed_message:
                        all_outputs.append(processed_message)
                    
                    # Add assistant response to history (include tool_calls if present)
                    assistant_entry = {"role": "assistant", "content": assistant_message}
                    if tool_calls:
                        assistant_entry["tool_calls"] = tool_calls
                    history.append(assistant_entry)
                    
                    # If done or no tools were used, we're finished
                    if is_done:
                        logger.info(f"Ollama agent completed after {iteration} iterations")
                        break
                    
                    if not tool_results:
                        # No tools used and not marked done - assume single response is enough
                        logger.info(f"Ollama agent finished (no tools used) after {iteration} iterations")
                        break
                    
                    # Feed tool results back to the model for next iteration
                    tool_feedback = "**Tool Results:**\n" + "\n\n".join(tool_results)
                    tool_feedback += "\n\nContinue with the task. Signal completion when done."
                    history.append({"role": "user", "content": tool_feedback})
                
                # Safety: if we hit max iterations
                if iteration >= self.MAX_AGENT_ITERATIONS:
                    all_outputs.append(f"\n⚠️ Reached maximum iterations ({self.MAX_AGENT_ITERATIONS}). Task may be incomplete.")
                
                # Keep history manageable (last 30 messages for agentic context)
                if len(history) > 30:
                    # Keep system prompt + recent messages
                    self._ollama_histories[history_key] = [history[0]] + history[-29:]
                
                # Combine all outputs
                final_output = "\n\n---\n\n".join(all_outputs) if len(all_outputs) > 1 else (all_outputs[0] if all_outputs else "")
                
                return final_output, None
                
        except httpx.ConnectError:
            return "", "Ollama not running. Start it with: ollama serve"
        except Exception as e:
            logger.error(f"Ollama error: {str(e)}")
            return "\n\n---\n\n".join(all_outputs) if all_outputs else "", f"Ollama error: {str(e)}"
    
    def _find_claude_cli(self) -> Optional[str]:
        """Find the Claude Code CLI executable."""
        # Try common names
        for name in ["claude", "claude-code", "claude.exe", "claude-code.exe"]:
            path = shutil.which(name)
            if path:
                return path
        return None
    
    def _get_agent_workdir(self, user_id: int, agent_id: int) -> Path:
        """Get or create a working directory for a specific agent."""
        # Each agent gets its own directory so --continue works per-agent
        agent_dir = self.working_dir / ".claude-agents" / f"user_{user_id}" / f"agent_{agent_id}"
        agent_dir.mkdir(parents=True, exist_ok=True)
        return agent_dir
    
    async def run_task(
        self,
        prompt: str,
        task_type: Optional[str] = None,
        user_id: Optional[int] = None,
        agent_id: int = 1,
        is_new_session: bool = False,
        chat_id: Optional[int] = None
    ) -> TaskResult:
        """
        Execute a task using Claude Code.
        
        Args:
            prompt: The task prompt/instruction.
            task_type: Optional task type for validation.
            user_id: User ID for logging.
            agent_id: Agent ID for multi-agent support.
            is_new_session: If True, start fresh conversation.
            chat_id: Telegram chat ID (for reminders).
            
        Returns:
            TaskResult with execution details.
        """
        created_at = datetime.now().isoformat()
        start_time = asyncio.get_event_loop().time()
        
        # Validate the prompt
        is_safe, matched_pattern = CommandValidator.check_forbidden_patterns(prompt)
        if not is_safe:
            return TaskResult(
                status=TaskStatus.REJECTED,
                output=None,
                error=f"Prompt contains forbidden pattern: {matched_pattern}",
                execution_time=0,
                task_type=task_type,
                created_at=created_at
            )
        
        # Validate task type if provided
        if task_type:
            is_valid, msg = CommandValidator.validate_task_type(task_type)
            if not is_valid:
                return TaskResult(
                    status=TaskStatus.REJECTED,
                    output=None,
                    error=msg,
                    execution_time=0,
                    task_type=task_type,
                    created_at=created_at
                )
        
        # Get agent-specific working directory
        # This ensures --continue works per-agent (each has separate conversation history)
        agent_workdir = self._get_agent_workdir(user_id, agent_id)
        
        # Get user's preferred model
        model = self.get_user_model(user_id) if user_id else self.default_model
        
        # ========================================
        # OLLAMA PATH - Use our custom handler with command execution
        # ========================================
        if self.is_ollama_model(model):
            logger.info(f"Running Ollama task for user {user_id} agent {agent_id} (model={model}): {prompt[:100]}...")
            
            output, error = await self._run_ollama_chat(
                prompt=prompt,
                model=model,
                user_id=user_id,
                agent_id=agent_id,
                is_new_session=is_new_session,
                chat_id=chat_id or user_id  # Fall back to user_id for DMs
            )
            
            execution_time = asyncio.get_event_loop().time() - start_time
            
            if error:
                return TaskResult(
                    status=TaskStatus.FAILED,
                    output=output if output else None,
                    error=error,
                    execution_time=execution_time,
                    task_type=task_type,
                    created_at=created_at
                )
            
            return TaskResult(
                status=TaskStatus.COMPLETED,
                output=output,
                error=None,
                execution_time=execution_time,
                task_type=task_type,
                created_at=created_at
            )
        
        # ========================================
        # CLAUDE PATH - Use Claude Code CLI
        # ========================================
        # Get agent-specific working directory
        agent_workdir = self._get_agent_workdir(user_id, agent_id)
        
        cli_model = model
        
        # Build the command for Claude Code CLI
        cmd = [
            self.claude_path,
            "--model", cli_model,  # Model name
            "--print",  # Non-interactive mode, print output
            "--output-format", "text",  # Plain text output
            "--dangerously-skip-permissions",  # Allow commands to execute
        ]
        
        # Handle conversation continuity
        if not is_new_session:
            # Continue this agent's most recent conversation
            cmd.append("--continue")
        # If is_new_session, don't add --continue (starts fresh)
        
        cmd.extend(["-p", prompt])
        
        logger.info(f"Running Claude Code task for user {user_id} agent {agent_id} (model={cli_model}): {prompt[:100]}...")
        
        # Use default environment for Claude models
        env = os.environ.copy()
        
        try:
            # Create subprocess in agent-specific directory
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(agent_workdir),
                env=env
            )
            
            task_id = f"{user_id}_{created_at}"
            self._running_tasks[task_id] = process
            
            try:
                # Wait with timeout
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
                
                execution_time = asyncio.get_event_loop().time() - start_time
                
                # Decode output
                output = stdout.decode("utf-8", errors="replace").strip()
                error = stderr.decode("utf-8", errors="replace").strip()
                
                # Debug logging
                logger.info(f"Claude Code exit code: {process.returncode}")
                logger.info(f"Claude Code stdout ({len(output)} chars): {output[:500] if output else '(empty)'}")
                logger.info(f"Claude Code stderr ({len(error)} chars): {error[:500] if error else '(empty)'}")
                
                if process.returncode == 0:
                    return TaskResult(
                        status=TaskStatus.COMPLETED,
                        output=output,
                        error=error if error else None,
                        execution_time=execution_time,
                        task_type=task_type,
                        created_at=created_at
                    )
                else:
                    return TaskResult(
                        status=TaskStatus.FAILED,
                        output=output if output else None,
                        error=error or f"Exit code: {process.returncode}",
                        execution_time=execution_time,
                        task_type=task_type,
                        created_at=created_at
                    )
                    
            except asyncio.TimeoutError:
                # Kill the process on timeout
                process.kill()
                await process.wait()
                
                execution_time = asyncio.get_event_loop().time() - start_time
                
                return TaskResult(
                    status=TaskStatus.TIMEOUT,
                    output=None,
                    error=f"Task exceeded timeout of {self.timeout}s",
                    execution_time=execution_time,
                    task_type=task_type,
                    created_at=created_at
                )
            finally:
                self._running_tasks.pop(task_id, None)
                
        except FileNotFoundError:
            return TaskResult(
                status=TaskStatus.FAILED,
                output=None,
                error=f"Claude Code CLI not found at: {self.claude_path}. "
                      f"Please install it or set CLAUDE_CODE_PATH in .env",
                execution_time=0,
                task_type=task_type,
                created_at=created_at
            )
        except Exception as e:
            execution_time = asyncio.get_event_loop().time() - start_time
            logger.error(f"Task execution error: {e}")
            return TaskResult(
                status=TaskStatus.FAILED,
                output=None,
                error=str(e),
                execution_time=execution_time,
                task_type=task_type,
                created_at=created_at
            )
    
    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        if task_id in self._running_tasks:
            process = self._running_tasks[task_id]
            process.kill()
            return True
        return False
    
    def get_running_tasks(self) -> list[str]:
        """Get list of currently running task IDs."""
        return list(self._running_tasks.keys())


class TaskQueue:
    """
    Manages a queue of tasks with rate limiting.
    Ensures only one task runs at a time per agent (not per user).
    """
    
    def __init__(self, runner: ClaudeCodeRunner, max_queue_size: int = 10):
        self.runner = runner
        self.max_queue_size = max_queue_size
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._agent_locks: dict[str, asyncio.Lock] = {}  # Per-agent locks
        self._history: list[TaskResult] = []
        self._max_history = 100
    
    def _get_agent_lock(self, user_id: int, agent_id: int = 1) -> asyncio.Lock:
        """Get or create a lock for a specific agent (user + agent combo)."""
        lock_key = f"{user_id}_{agent_id}"
        if lock_key not in self._agent_locks:
            self._agent_locks[lock_key] = asyncio.Lock()
        return self._agent_locks[lock_key]
    
    async def submit_task(
        self,
        prompt: str,
        user_id: int,
        task_type: Optional[str] = None,
        agent_id: int = 1,
        is_new_session: bool = False,
        chat_id: Optional[int] = None
    ) -> TaskResult:
        """
        Submit a task for execution.
        Ensures only one task runs at a time per agent (allows parallel agents).
        """
        lock = self._get_agent_lock(user_id, agent_id)
        
        async with lock:
            result = await self.runner.run_task(
                prompt=prompt,
                task_type=task_type,
                user_id=user_id,
                agent_id=agent_id,
                is_new_session=is_new_session,
                chat_id=chat_id
            )
            
            # Store in history
            self._history.append(result)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            
            return result
    
    def get_history(self, user_id: Optional[int] = None, limit: int = 10) -> list[TaskResult]:
        """Get task history, optionally filtered by user."""
        history = self._history[-limit:]
        return list(reversed(history))


def create_task_queue(settings) -> TaskQueue:
    """Factory function to create TaskQueue from settings."""
    runner = ClaudeCodeRunner(
        working_dir=settings.working_directory,
        claude_path=settings.claude_code_path,
        timeout=settings.task_timeout,
        default_model=settings.claude_model
    )
    return TaskQueue(runner)
