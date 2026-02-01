"""
Main Telegram bot application.
Combines FastAPI server with Telegram bot handlers.
Supports multiple parallel Claude Code agents.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
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

from config import load_settings, ALLOWED_TASK_TYPES
from security import create_security_manager, CommandValidator, SecurityManager
from transcriber import create_voice_handler, VoiceHandler
from claude_runner import create_task_queue, TaskQueue
from agent_manager import AgentManager, parse_agent_command

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

**Usage:**
- Plain messages go to Agent 1 (default)
- Use /N prefix to talk to specific agents
- Each agent has separate memory/context

**Task Types:**
/code, /explain, /review, /debug, /test, /docs, /refactor

**Voice Notes:**
Send a voice message - it will be transcribed and processed.
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
    
    await process_task(update, message, agent_id=agent_id)


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
    
    await process_task(update, content, task_type=command)


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
    
    # Show typing indicator
    await update.message.chat.send_action(ChatAction.TYPING)
    
    # Send processing message with agent info
    agent_label = f"Agent {agent_id}" if agent_id > 1 else "Agent 1"
    new_label = " (new)" if is_new else ""
    
    processing_msg = await update.message.reply_text(
        f"[{agent_label}{new_label}] Processing...\n"
        f"Task: `{task_type or 'general'}`",
        parse_mode=ParseMode.MARKDOWN
    )
    
    try:
        # Execute task with agent's directory (each agent has separate conversation)
        result = await task_queue.submit_task(
            prompt=text,
            user_id=user_id,
            task_type=task_type,
            agent_id=agent_id,
            is_new_session=is_new
        )
        
        # Update agent usage
        agent_manager.touch_agent(user_id, agent_id)
        
        # Format and send result
        response = result.to_telegram_message()
        
        # Add agent label to response
        response = f"**[{agent_label}]**\n{response}"
        
        # Delete processing message and send result
        try:
            await processing_msg.delete()
        except Exception:
            pass
        
        await update.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Task processing error: {e}")
        await processing_msg.edit_text(
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
        await process_task(update, message, agent_id=agent_id)
    else:
        # Default agent (1)
        await process_task(update, text)


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
            await process_task(update, message, agent_id=agent_id)
        else:
            await process_task(update, result.text)
        
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
        filters.VOICE,
        handle_voice_message
    ))


async def set_bot_commands(app: Application) -> None:
    """Set bot commands for Telegram menu."""
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "Show help"),
        BotCommand("agents", "List active agents"),
        BotCommand("new", "Create new agent"),
        BotCommand("model", "View/change LLM model"),
        BotCommand("terminate", "Close an agent"),
        BotCommand("wipe", "Clear all agents & chat"),
        BotCommand("status", "Check session status"),
        BotCommand("cancel", "Cancel running task"),
    ]
    await app.bot.set_my_commands(commands)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for startup/shutdown."""
    global settings, security_manager, voice_handler, task_queue, agent_manager, telegram_app
    
    logger.info("Starting up...")
    
    # Load configuration
    settings = load_settings()
    
    # Initialize components
    security_manager = create_security_manager(settings)
    voice_handler = create_voice_handler(settings)
    task_queue = create_task_queue(settings)
    agent_manager = AgentManager()
    
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
