"""
Main Telegram bot application.
Combines FastAPI server with Telegram bot handlers.
Supports multiple parallel Claude Code agents.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest

from config import load_settings, ALLOWED_TASK_TYPES
from security import create_security_manager, CommandValidator, SecurityManager
from transcriber import create_voice_handler, VoiceHandler
from claude_runner import create_task_queue, TaskQueue
from agent_manager import AgentManager, parse_agent_command
from scheduler import ReminderScheduler, set_scheduler, get_scheduler
from brave_search import create_brave_search_client, BraveSearchClient
from memory_cache import MemoryCache, SessionCache, DailyScratchpad
from markdown_utils import sanitize_markdown_for_telegram, strip_all_markdown

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global instances (initialized on startup)
settings = None
security_manager: Optional[SecurityManager] = None
voice_handler: Optional[VoiceHandler] = None
task_queue: Optional[TaskQueue] = None
agent_manager: Optional[AgentManager] = None
telegram_app: Optional[Application] = None
reminder_scheduler: Optional[ReminderScheduler] = None
brave_search: Optional[BraveSearchClient] = None

# Caching system (PERSONA_CACHE pattern)
memory_cache: Optional[MemoryCache] = None
session_cache: Optional[SessionCache] = None
daily_scratchpad: Optional[DailyScratchpad] = None

# Lock for atomic disk writes
_disk_lock = asyncio.Lock()

MEMORY_FILE = Path("data") / "MEMORY.md"


async def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a temp file + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    await asyncio.to_thread(tmp.write_text, content, encoding="utf-8")
    await asyncio.to_thread(tmp.rename, path)


# ============================================================================
# Helper Functions
# ============================================================================

async def _keep_typing(chat, bot):
    """Continuously send typing indicator every 4 seconds."""
    try:
        while True:
            await chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(4)  # Send every 4 seconds (expires after 5)
    except asyncio.CancelledError:
        pass  # Task was cancelled, stop typing


# ============================================================================
# Telegram Command Handlers
# ============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command - show welcome message."""
    user = update.effective_user
    user_id = user.id
    
    if not security_manager.is_user_allowed(user_id):
        await update.message.reply_text(
            "Sorry, you're not authorized to use this bot.\n"
            f"Your user ID: `{user_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if security_manager.is_user_paired(user_id):
        await update.message.reply_text(
            f"Welcome back, {user.first_name}!\n\n"
            "Send me a message or voice note, and I'll process it with Claude Code.\n\n"
            "**Commands:**\n"
            "/help - Show available commands\n"
            "/agents - List your active agents\n"
            "/new - Create a new agent\n"
            "/terminate N - Close agent N\n"
            "/2 message - Send to agent 2\n"
            "/3 message - Send to agent 3",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"Hi {user.first_name}!\n\n"
            "To use this bot, you need to complete pairing first.\n\n"
            "Use: `/pair YOUR_SECRET`\n\n"
            "Replace `YOUR_SECRET` with the pairing secret from your server config.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_pair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pair command - pair user with secret."""
    user = update.effective_user
    user_id = user.id
    
    if not context.args:
        await update.message.reply_text(
            "Please provide the pairing secret:\n"
            "`/pair YOUR_SECRET`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    secret = " ".join(context.args)
    success, message = security_manager.verify_pairing(
        user_id=user_id,
        secret=secret,
        username=user.username
    )
    
    await update.message.reply_text(message)
    
    # Delete the message containing the secret for security
    try:
        await update.message.delete()
    except Exception:
        pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command - show help information."""
    help_text = """
**Claude Code Telegram Bot**

**Basic Commands:**
/start - Welcome message
/pair <secret> - Pair your account
/help - This help message
/status - Check your session
/cancel - Cancel running task
/search <query> - Search the web with Brave

**Multi-Agent Commands:**
/agents - List all your active agents
/new - Create a new agent
/terminate N - Close agent N (e.g. /terminate 2)
/wipe - Clear ALL agents and chat history
/2 message - Send message to agent 2
/3 message - Send message to agent 3

**Model Selection:**
/model - View current model & options
/model sonnet - Switch to Sonnet
/model opus - Switch to Opus

**Session & Memory:**
/session clear - Wipe your session data
/memory promote <text> - Append a note to MEMORY.md

**Reminders:**
/reminders - List your scheduled reminders
/reminders cancel <id> - Cancel a reminder
Ask naturally: "Remind me to X in 30 minutes"

**Usage:**
- Plain messages go to Agent 1 (default)
- Use /N prefix to talk to specific agents
- Each agent has separate memory/context

**Task Types:**
/code, /explain, /review, /debug, /test, /docs, /refactor

**Media Support:**
📷 Photos - Send an image for AI analysis (with optional caption)
🎤 Voice - Send a voice message - it will be transcribed and processed
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command - show user status."""
    user_id = update.effective_user.id
    
    if not security_manager.is_user_allowed(user_id):
        await update.message.reply_text("Not authorized.")
        return
    
    user_info = security_manager.get_user_info(user_id)
    
    if user_info:
        # Get agent info
        agents = agent_manager.get_active_agents(user_id)
        agent_list = "\n".join([
            f"  - Agent {a.agent_id}: {a.message_count} msgs"
            for a in agents
        ]) or "  None active"
        
        status_text = f"""
**Session Active**

User ID: `{user_info.user_id}`
Username: @{user_info.username or 'N/A'}
Paired: {user_info.paired_at[:10]}
Last Active: {user_info.last_active[:19]}

**Active Agents:**
{agent_list}

Working Dir: `{settings.working_directory}`
Timeout: {settings.task_timeout}s
"""
    else:
        status_text = f"""
**Not Paired**

Your user ID `{user_id}` is allowlisted but not paired.
Use `/pair YOUR_SECRET` to complete setup.
"""
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tasks command - show available task types."""
    lines = ["**Available Task Types:**\n"]
    
    for task_type, description in ALLOWED_TASK_TYPES.items():
        lines.append(f"  `/{task_type}` - {description}")
    
    lines.append("\nYou can also just send a plain message!")
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel command - cancel running task."""
    user_id = update.effective_user.id
    running = task_queue.runner.get_running_tasks()
    
    user_tasks = [t for t in running if t.startswith(f"{user_id}_")]
    
    if not user_tasks:
        await update.message.reply_text("No running tasks to cancel.")
        return
    
    for task_id in user_tasks:
        await task_queue.runner.cancel_task(task_id)
    
    await update.message.reply_text(f"Cancelled {len(user_tasks)} task(s).")


async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /agents command - list active agents."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    agents = agent_manager.get_active_agents(user_id)
    
    if not agents:
        await update.message.reply_text(
            "**No active agents**\n\n"
            "Send a message to start Agent 1, or use `/new` to create one.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    lines = ["**Your Active Agents:**\n"]
    for agent in sorted(agents, key=lambda a: a.agent_id):
        lines.append(
            f"**Agent {agent.agent_id}** ({agent.name})\n"
            f"  Messages: {agent.message_count}\n"
            f"  Last used: {agent.last_used[:19]}\n"
        )
    
    lines.append(f"\nMax agents: {agent_manager.MAX_AGENTS_PER_USER}")
    lines.append("Use `/N message` to talk to agent N")
    lines.append("Use `/terminate N` to close agent N")
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command - create a new agent."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    try:
        agent = agent_manager.create_new_agent(user_id)
        await update.message.reply_text(
            f"**Created Agent {agent.agent_id}**\n\n"
            f"Use `/{agent.agent_id} your message` to talk to it.\n"
            f"This agent has fresh memory (no previous context).",
            parse_mode=ParseMode.MARKDOWN
        )
    except ValueError as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_terminate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /terminate command - close an agent."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Please specify which agent to terminate:\n"
            "`/terminate 2` - closes agent 2",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    agent_id = int(context.args[0])
    
    if agent_manager.terminate_agent(user_id, agent_id):
        await update.message.reply_text(
            f"**Agent {agent_id} terminated**\n"
            f"Its conversation history has been cleared.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(f"Agent {agent_id} not found or already terminated.")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command - view or change the LLM model."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    available = task_queue.runner.AVAILABLE_MODELS
    current = task_queue.runner.get_user_model(user_id)
    
    if not context.args:
        # Show current model and available options
        models_list = "\n".join([f"  `{short}` = {full}" for short, full in available.items()])
        await update.message.reply_text(
            f"**Current model:** `{current}`\n\n"
            f"**Available shortcuts:**\n{models_list}\n\n"
            f"**Change with:** `/model sonnet` or `/model opus`\n"
            f"Or use full name: `/model claude-sonnet-4-20250514`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Set new model
    model_input = context.args[0]
    new_model = task_queue.runner.set_user_model(user_id, model_input)
    
    await update.message.reply_text(
        f"**Model changed**\n\nNow using: `{new_model}`",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_wipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /wipe command - clear all agents and chat history."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    # Get all active agents
    agents = agent_manager.get_active_agents(user_id)
    agent_count = len(agents)
    
    # Terminate all agents (clears their conversation memory)
    for agent in agents:
        agent_manager.terminate_agent(user_id, agent.agent_id)
    
    # Clear Ollama conversation histories for this user
    keys_to_delete = [k for k in task_queue.runner._ollama_histories.keys() if k.startswith(f"{user_id}_")]
    for key in keys_to_delete:
        del task_queue.runner._ollama_histories[key]
    
    # Delete chat messages in parallel (much faster)
    chat_id = update.effective_chat.id
    message_id = update.message.message_id
    
    # Create delete tasks for last 50 messages
    async def try_delete(msg_id):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            return True
        except Exception:
            return False
    
    # Run all deletes in parallel
    tasks = [try_delete(message_id - i) for i in range(50)]
    results = await asyncio.gather(*tasks)
    deleted_count = sum(results)
    
    # Send confirmation
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"**WIPED**\n\n"
             f"Cleared {agent_count} agent(s)\n"
             f"Deleted {deleted_count} message(s)\n\n"
             f"Fresh start!",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /search command - search the web using Brave Search."""
    user_id = update.effective_user.id

    if not await check_authorization(update, user_id):
        return

    if not brave_search:
        await update.message.reply_text("Search is not available (API key not configured).")
        return

    if not context.args:
        await update.message.reply_text(
            "Please provide a search query:\n"
            "`/search your query here`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    query = " ".join(context.args)

    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        # Perform search
        results = await brave_search.web_search(query, count=5)

        # Format results
        formatted = brave_search.format_results_for_telegram(results, max_results=5)

        await update.message.reply_text(
            formatted,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Search error: {e}")
        await update.message.reply_text(f"Search failed: {str(e)[:200]}")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reminders command - list and manage scheduled reminders."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if not await check_authorization(update, user_id):
        return
    
    scheduler = get_scheduler()
    if not scheduler:
        await update.message.reply_text("Reminder system is not available.")
        return
    
    # Check for cancel subcommand: /reminders cancel <job_id>
    if context.args and len(context.args) >= 2 and context.args[0].lower() == "cancel":
        job_id = context.args[1]
        if scheduler.cancel_reminder(job_id):
            await update.message.reply_text(
                f"✅ Cancelled reminder `{job_id}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                f"❌ Reminder `{job_id}` not found",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    # List reminders for this chat
    reminders = scheduler.get_reminders(chat_id)
    
    if not reminders:
        await update.message.reply_text(
            "**No scheduled reminders**\n\n"
            "Ask me to set a reminder, for example:\n"
            "• \"Remind me to check the oven in 30 minutes\"\n"
            "• \"Remind me to call mom at 3pm tomorrow\"\n"
            "• \"Remind me every Monday at 9am about the standup\"",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    lines = ["**Your Scheduled Reminders:**\n"]
    for r in reminders:
        trigger_type = r['trigger_type'].replace('Trigger', '')
        next_run = r['next_run'][:19] if r['next_run'] else 'N/A'
        lines.append(
            f"**{r['name']}**\n"
            f"  ID: `{r['id']}`\n"
            f"  Type: {trigger_type}\n"
            f"  Next: {next_run}\n"
        )
    
    lines.append("\n**To cancel:** `/reminders cancel <id>`")
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /session command — manage session data."""
    user_id = update.effective_user.id

    if not await check_authorization(update, user_id):
        return

    if not context.args or context.args[0].lower() != "clear":
        await update.message.reply_text(
            "**Session Commands:**\n"
            "`/session clear` — Delete your session data and start fresh",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    async with _disk_lock:
        agent_manager.clear_user_sessions(user_id)

    await update.message.reply_text(
        "Session cleared. All agent history wiped.\n"
        "Send a message to start fresh.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /memory command — manage persistent MEMORY.md."""
    user_id = update.effective_user.id

    if not await check_authorization(update, user_id):
        return

    if not context.args or context.args[0].lower() != "promote":
        await update.message.reply_text(
            "**Memory Commands:**\n"
            "`/memory promote <text>` — Append a note to MEMORY.md",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Please provide text to promote:\n"
            "`/memory promote your note here`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    text = " ".join(context.args[1:]).strip()

    # Use cached memory system
    async with _disk_lock:
        await memory_cache.append_to_memory(text)

    # Also log to daily scratchpad
    await daily_scratchpad.append(f"Memory promoted: {text}")

    await update.message.reply_text(
        f"Added to MEMORY.md:\n`{text}`",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /persona command — reload memory cache from disk."""
    user_id = update.effective_user.id

    if not await check_authorization(update, user_id):
        return

    if not context.args or context.args[0].lower() != "reload":
        await update.message.reply_text(
            "**Persona Commands:**\n"
            "`/persona reload` — Refresh MEMORY, SOUL, USER from disk",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    async with _disk_lock:
        await memory_cache.reload()

    await update.message.reply_text(
        "✅ Memory cache reloaded from disk.\n"
        "MEMORY.md, SOUL.md, and USER.md are now up to date.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /N commands where N is an agent number."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    # Parse the command - format is /N message
    text = update.message.text
    match = re.match(r'^/(\d+)\s*(.*)', text, re.DOTALL)
    
    if not match:
        return
    
    agent_id = int(match.group(1))
    message = match.group(2).strip()
    
    if not message:
        await update.message.reply_text(
            f"Please provide a message for Agent {agent_id}:\n"
            f"`/{agent_id} your message here`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    await process_task(update, context, message, agent_id=agent_id)


# Task type commands
async def cmd_task_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle task type commands like /code, /explain, etc."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    # Get task type from command
    command = update.message.text.split()[0][1:]  # Remove /
    content = " ".join(context.args) if context.args else ""
    
    if not content:
        await update.message.reply_text(
            f"Please provide content for the `/{command}` command.\n"
            f"Example: `/{command} Write a hello world function`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    await process_task(update, context, content, task_type=command)


# ============================================================================
# Message Handlers
# ============================================================================

async def check_authorization(update: Update, user_id: int) -> bool:
    """Check if user is authorized and paired."""
    if not security_manager.is_user_allowed(user_id):
        await update.message.reply_text(
            f"Not authorized. Your ID: `{user_id}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return False
    
    if not security_manager.is_user_paired(user_id):
        await update.message.reply_text(
            "Please complete pairing first with `/pair YOUR_SECRET`",
            parse_mode=ParseMode.MARKDOWN
        )
        return False
    
    return True


async def process_task(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    text: str, 
    task_type: Optional[str] = None,
    agent_id: Optional[int] = None
) -> None:
    """Process a task request, routing to the appropriate agent."""
    user_id = update.effective_user.id
    
    # Update activity
    security_manager.update_activity(user_id)
    
    # Sanitize input
    text = CommandValidator.sanitize_input(text)
    
    if not text:
        await update.message.reply_text("Empty message. Please provide a task.")
        return
    
    # Parse command if no task type specified
    if not task_type:
        parsed_type, content = CommandValidator.parse_command(text)
        if parsed_type:
            task_type = parsed_type
            text = content
    
    # Default to agent 1 if not specified
    if agent_id is None:
        agent_id = 1
    
    # Get or create the agent
    try:
        agent, is_new = agent_manager.get_or_create_agent(user_id, agent_id)
    except ValueError as e:
        await update.message.reply_text(f"Error: {e}")
        return
    
    # Start continuous typing indicator
    typing_task = asyncio.create_task(_keep_typing(update.message.chat, context.bot))

    try:
        # Execute task with agent's directory (each agent has separate conversation)
        result = await task_queue.submit_task(
            prompt=text,
            user_id=user_id,
            task_type=task_type,
            agent_id=agent_id,
            is_new_session=is_new,
            chat_id=update.effective_chat.id  # Pass chat_id for reminder scheduling
        )

        # Stop typing indicator
        typing_task.cancel()

        # Update agent usage
        agent_manager.touch_agent(user_id, agent_id)

        # Format and send result with agent_id in footer
        response = result.to_telegram_message(agent_id=agent_id)

        # Sanitize markdown for Telegram compatibility
        sanitized = sanitize_markdown_for_telegram(response)

        try:
            await update.message.reply_text(
                sanitized,
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            logger.warning(f"Markdown parse failed ({e}); retrying with plain text.")
            # Fallback: strip all markdown and send plain text
            plain_text = strip_all_markdown(response)
            await update.message.reply_text(plain_text)

    except Exception as e:
        # Stop typing indicator
        typing_task.cancel()
        logger.error(f"Task processing error: {e}")
        await update.message.reply_text(
            f"Error processing task: {str(e)[:200]}"
        )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages."""
    user_id = update.effective_user.id
    
    if not await check_authorization(update, user_id):
        return
    
    text = update.message.text
    
    # Check for agent routing pattern /N message
    agent_id, message = parse_agent_command(text)
    
    if agent_id is not None:
        # Route to specific agent
        await process_task(update, context, message, agent_id=agent_id)
    else:
        # Default agent (1)
        await process_task(update, context, text)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photo messages."""
    user_id = update.effective_user.id

    if not await check_authorization(update, user_id):
        return

    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)

    # Get the highest resolution photo
    photo = update.message.photo[-1]

    # Get caption if provided
    caption = update.message.caption or "What's in this image?"

    analyzing_msg = await update.message.reply_text("📸 Analyzing image...")

    try:
        # Download the photo
        file = await context.bot.get_file(photo.file_id)

        # Create temp directory for images
        temp_dir = Path(settings.working_directory) / "data" / "temp_images"
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Save image with unique name
        image_path = temp_dir / f"{user_id}_{photo.file_id}.jpg"
        await file.download_to_drive(str(image_path))

        # Create prompt with image reference
        prompt = f"Analyze this image: {image_path}\n\nUser question: {caption}"

        # Update message
        await analyzing_msg.edit_text("🔍 Processing with AI...")

        # Process with AI (will read the image)
        await process_task(update, context, prompt)

        # Delete analyzing message
        try:
            await analyzing_msg.delete()
        except Exception:
            pass

        # Clean up image file
        try:
            image_path.unlink()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        await analyzing_msg.edit_text(f"Error: {str(e)[:200]}")


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming voice messages."""
    user_id = update.effective_user.id

    if not await check_authorization(update, user_id):
        return
    
    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)
    
    # Send transcribing message
    transcribing_msg = await update.message.reply_text("Transcribing voice message...")
    
    try:
        # Get voice file
        voice = update.message.voice
        
        # Transcribe
        result = await voice_handler.process_voice_message(
            bot=context.bot,
            file_id=voice.file_id
        )
        
        if not result.success:
            await transcribing_msg.edit_text(
                f"Transcription failed: {result.error}"
            )
            return
        
        # Update message with transcription
        await transcribing_msg.edit_text(
            f"Transcribed ({result.language or 'unknown'}):\n"
            f"_{result.text}_\n\n"
            "Processing...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Check if transcription contains agent routing
        agent_id, message = parse_agent_command(result.text)
        
        if agent_id is not None:
            await process_task(update, context, message, agent_id=agent_id)
        else:
            await process_task(update, context, result.text)
        
        # Delete transcription message
        try:
            await transcribing_msg.delete()
        except Exception:
            pass
            
    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await transcribing_msg.edit_text(f"Error: {str(e)[:200]}")


# ============================================================================
# Application Setup
# ============================================================================

def setup_handlers(app: Application) -> None:
    """Register all command and message handlers."""
    
    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pair", cmd_pair))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    
    # Multi-agent commands
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("terminate", cmd_terminate))
    app.add_handler(CommandHandler("wipe", cmd_wipe))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("persona", cmd_persona))
    
    # Task type commands
    for task_type in ALLOWED_TASK_TYPES:
        app.add_handler(CommandHandler(task_type, cmd_task_type))
    
    # Agent number commands (/2, /3, etc.) - use regex filter
    app.add_handler(MessageHandler(
        filters.Regex(r'^/\d+\s'),
        cmd_agent_message
    ))
    
    # Message handlers
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_message
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO,
        handle_photo_message
    ))
    app.add_handler(MessageHandler(
        filters.VOICE,
        handle_voice_message
    ))


async def set_bot_commands(app: Application) -> None:
    """Set bot commands for Telegram menu."""
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "Show help"),
        BotCommand("search", "Search the web"),
        BotCommand("agents", "List active agents"),
        BotCommand("new", "Create new agent"),
        BotCommand("model", "View/change LLM model"),
        BotCommand("terminate", "Close an agent"),
        BotCommand("reminders", "List/cancel reminders"),
        BotCommand("wipe", "Clear all agents & chat"),
        BotCommand("status", "Check session status"),
        BotCommand("cancel", "Cancel running task"),
        BotCommand("session", "Manage session data"),
        BotCommand("memory", "Promote a note to MEMORY.md"),
        BotCommand("persona", "Reload memory cache"),
    ]
    await app.bot.set_my_commands(commands)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for startup/shutdown."""
    global settings, security_manager, voice_handler, task_queue, agent_manager, telegram_app, reminder_scheduler, brave_search, memory_cache, session_cache, daily_scratchpad

    logger.info("Starting up...")

    # Load configuration
    settings = load_settings()

    # Initialize caching system (PERSONA_CACHE pattern)
    memory_dir = settings.working_directory / "memory"
    data_dir = settings.working_directory / "data"

    memory_cache = MemoryCache(memory_dir)
    await memory_cache.load()
    logger.info("Memory cache loaded")

    session_cache = SessionCache(data_dir, flush_interval=5.0)
    session_cache.start()
    logger.info("Session cache started")

    daily_scratchpad = DailyScratchpad(memory_dir)
    logger.info("Daily scratchpad initialized")

    # Initialize components
    security_manager = create_security_manager(settings)
    voice_handler = create_voice_handler(settings)
    agent_manager = AgentManager()

    # Initialize Brave Search if API key is provided
    if settings.brave_search_api_key:
        brave_search = create_brave_search_client(settings.brave_search_api_key)
        logger.info("Brave Search initialized")
    else:
        brave_search = None
        logger.warning("Brave Search API key not configured")

    # Create task queue with Brave Search integration
    task_queue = create_task_queue(settings, brave_search_client=brave_search)
    
    # Create Telegram application
    telegram_app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    
    # Setup handlers
    setup_handlers(telegram_app)
    
    # Initialize the application
    await telegram_app.initialize()
    await set_bot_commands(telegram_app)
    
    # Initialize reminder scheduler with Telegram send callback
    async def send_reminder_message(chat_id: int, message: str) -> None:
        """Callback to send reminder messages via Telegram."""
        try:
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send reminder to {chat_id}: {e}")
    
    reminder_scheduler = ReminderScheduler(
        send_callback=send_reminder_message,
        db_path=str(settings.working_directory / "data" / "reminders.db")
    )
    reminder_scheduler.start()
    set_scheduler(reminder_scheduler)
    logger.info("Reminder scheduler initialized")
    
    # Make scheduler available to task_queue runner for AI tool access
    task_queue.runner.reminder_scheduler = reminder_scheduler
    
    if settings.use_webhook:
        # Webhook mode
        webhook_url = f"{settings.webhook_url}/webhook"
        await telegram_app.bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to: {webhook_url}")
    else:
        # Polling mode
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Started polling for updates...")
    
    logger.info("Bot is ready!")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")

    # Stop session cache (flushes dirty sessions)
    if session_cache:
        session_cache.stop()
        logger.info("Session cache stopped and flushed")

    # Stop scheduler
    if reminder_scheduler:
        reminder_scheduler.shutdown()
        logger.info("Reminder scheduler stopped")
    
    if settings.use_webhook:
        await telegram_app.bot.delete_webhook()
    else:
        await telegram_app.updater.stop()
        await telegram_app.stop()
    
    await telegram_app.shutdown()
    logger.info("Shutdown complete.")


# Create FastAPI app
app = FastAPI(
    title="Claude Code Telegram Bot",
    description="Telegram bot for executing Claude Code tasks with multi-agent support",
    lifespan=lifespan
)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "status": "running",
        "bot": "Claude Code Telegram Bot"
    }


@app.get("/health")
async def health():
    """Detailed health check."""
    return {
        "status": "healthy",
        "webhook_mode": settings.use_webhook if settings else None,
        "working_dir": str(settings.working_directory) if settings else None
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Handle incoming Telegram webhook updates."""
    if not settings.use_webhook:
        raise HTTPException(status_code=400, detail="Webhook mode not enabled")
    
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    
    # Process update
    await telegram_app.process_update(update)
    
    return {"ok": True}


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Run the bot server."""
    import uvicorn
    
    # Load settings for port
    settings = load_settings()
    
    uvicorn.run(
        "bot:app",
        host="0.0.0.0",
        port=settings.server_port,
        reload=False,
        log_level="info"
    )


if __name__ == "__main__":
    main()
