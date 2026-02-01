# 🤖 Claude Code Telegram Bot

A secure Telegram bot that runs on your PC and lets you execute Claude Code tasks via text or voice messages.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Telegram   │────▶│  Your PC    │────▶│ Claude Code │
│  (You)      │◀────│  (FastAPI)  │◀────│   CLI       │
└─────────────┘     └─────────────┘     └─────────────┘
      │                    │
      │    Voice Notes     │
      └────────────────────┘
              │
              ▼
         ┌─────────┐
         │ Whisper │
         │  (STT)  │
         └─────────┘
```

## ✨ Features

- **Text & Voice**: Send text messages or voice notes
- **Local Processing**: Everything runs on your PC - your code never leaves
- **Voice Transcription**: Uses OpenAI Whisper for accurate voice-to-text
- **Security First**: User allowlist, pairing codes, command validation
- **Task Types**: Specialized commands for code, review, debug, explain, etc.
- **Async Processing**: Non-blocking task execution with timeouts

## 🔐 Security Features

| Feature | Description |
|---------|-------------|
| **User Allowlist** | Only specific Telegram user IDs can interact |
| **Pairing Required** | First-time users must enter a secret code |
| **Command Validation** | Dangerous patterns are blocked |
| **Shell Allowlist** | Only pre-approved shell commands execute |
| **Session Tracking** | All activity is logged with timestamps |

## 📋 Prerequisites

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- Telegram account
- ~2GB disk space (for Whisper model)

## 🚀 Quick Start

### 1. Create Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow prompts
3. Copy the bot token (looks like `123456789:ABCdefGHI...`)

### 2. Get Your User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. Copy your user ID (a number like `123456789`)

### 3. Clone & Configure

```bash
# Navigate to the bot directory
cd telegram_claude_bot

# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 4. Create `.env` File

Create a file named `.env` in the `telegram_claude_bot` folder:

```env
# ===========================================
# Telegram Bot Configuration
# ===========================================

# Get from @BotFather on Telegram
TELEGRAM_BOT_TOKEN=your_bot_token_here

# Your Telegram user ID (get from @userinfobot)
# Multiple IDs can be comma-separated: 123456789,987654321
ALLOWED_USER_IDS=your_user_id_here

# ===========================================
# Security Settings
# ===========================================

# Secret for first-time pairing (change this!)
PAIRING_SECRET=my_super_secret_code_12345

# ===========================================
# Server Configuration
# ===========================================

# Webhook mode (set to false for polling - easier for local dev)
USE_WEBHOOK=false

# Local server port
SERVER_PORT=8443

# ===========================================
# Claude Code Configuration
# ===========================================

# Path to Claude Code CLI (leave empty to use system PATH)
CLAUDE_CODE_PATH=

# Working directory for Claude Code tasks
CLAUDE_WORKING_DIR=C:\Users\YourName\Projects

# Task timeout in seconds
TASK_TIMEOUT=300

# ===========================================
# Whisper Configuration
# ===========================================

# Whisper model size: tiny, base, small, medium, large
# Smaller = faster, larger = more accurate
WHISPER_MODEL=base

# Device: cpu, cuda (for GPU)
WHISPER_DEVICE=cpu
```

### 5. Run the Bot

```bash
python bot.py
```

### 6. Pair Your Account

1. Open your bot in Telegram (search for your bot's username)
2. Send `/start`
3. Send `/pair YOUR_PAIRING_SECRET` (the secret from your `.env`)
4. You're ready to go!

## 📖 Usage

### Basic Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show help |
| `/status` | Check your session |
| `/tasks` | List available task types |
| `/cancel` | Cancel running task |

### Task Commands

```
/code Write a Python function to parse JSON
/explain How does async/await work?
/review Check this code for security issues
/debug Why is this loop not terminating?
/test Write unit tests for the auth module
/docs Generate docstrings for my functions
/refactor Clean up this messy function
```

### Just Send a Message

You don't need to use commands! Just describe what you want:

```
Write a function that validates email addresses
```

### Voice Notes

Hold the microphone button in Telegram and speak your request. The bot will:
1. Download the audio
2. Transcribe it with Whisper
3. Send the transcription to Claude Code
4. Reply with the result

## ⚙️ Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | - | Bot token from @BotFather |
| `ALLOWED_USER_IDS` | ✅ | - | Comma-separated Telegram user IDs |
| `PAIRING_SECRET` | ✅ | - | Secret code for first-time pairing |
| `USE_WEBHOOK` | ❌ | `false` | Use webhook instead of polling |
| `WEBHOOK_URL` | ❌ | - | Public URL for webhook |
| `SERVER_PORT` | ❌ | `8443` | Local server port |
| `CLAUDE_CODE_PATH` | ❌ | - | Path to Claude CLI (or use PATH) |
| `CLAUDE_WORKING_DIR` | ❌ | Current dir | Working directory for tasks |
| `TASK_TIMEOUT` | ❌ | `300` | Task timeout in seconds |
| `WHISPER_MODEL` | ❌ | `base` | Whisper model size |
| `WHISPER_DEVICE` | ❌ | `cpu` | `cpu` or `cuda` |

### Whisper Models

| Model | Size | Speed | Accuracy | VRAM |
|-------|------|-------|----------|------|
| `tiny` | 39M | Fastest | Basic | ~1GB |
| `base` | 74M | Fast | Good | ~1GB |
| `small` | 244M | Medium | Better | ~2GB |
| `medium` | 769M | Slow | Great | ~5GB |
| `large` | 1550M | Slowest | Best | ~10GB |

## 🔧 Advanced Setup

### Webhook Mode (for servers)

If you're running on a server with a public IP/domain:

1. Set up HTTPS (required by Telegram)
2. Update `.env`:
   ```env
   USE_WEBHOOK=true
   WEBHOOK_URL=https://your-domain.com
   ```

### Adding More Allowed Users

Add their Telegram user IDs to `ALLOWED_USER_IDS`:

```env
ALLOWED_USER_IDS=123456789,987654321,111222333
```

Each user must complete pairing with `/pair SECRET`.

### Customizing Allowed Commands

Edit `config.py` to modify:

```python
# Allowed task types
ALLOWED_TASK_TYPES = {
    "code": "Write or modify code",
    # Add more...
}

# Allowed shell commands
ALLOWED_SHELL_COMMANDS = {
    "git status",
    # Add more...
}

# Forbidden patterns (NEVER allow these)
FORBIDDEN_PATTERNS = [
    "rm -rf",
    # Add more...
]
```

## 🛡️ Security Best Practices

1. **Keep your pairing secret strong** - Use a random string like `openssl rand -hex 16`
2. **Limit allowed users** - Only add trusted user IDs
3. **Review forbidden patterns** - Add any dangerous commands for your environment
4. **Use a dedicated working directory** - Don't run from your root folder
5. **Monitor logs** - Check who's doing what
6. **Rotate secrets periodically** - Change the pairing secret and re-pair

## 🐛 Troubleshooting

### "Claude Code CLI not found"

Make sure Claude Code is installed and in your PATH:
```bash
claude --version
```

Or set the full path in `.env`:
```env
CLAUDE_CODE_PATH=C:\Users\YourName\AppData\Local\Programs\claude\claude.exe
```

### "Whisper model loading failed"

1. Ensure you have enough disk space
2. Try a smaller model: `WHISPER_MODEL=tiny`
3. On first run, the model downloads automatically

### "Not authorized" error

1. Check your user ID with @userinfobot
2. Verify it's in `ALLOWED_USER_IDS`
3. Complete pairing with `/pair SECRET`

### Voice notes not working

1. Install FFmpeg for audio conversion:
   - Windows: `choco install ffmpeg` or download from ffmpeg.org
   - Mac: `brew install ffmpeg`
   - Linux: `apt install ffmpeg`

## 📝 License

MIT License - feel free to modify and share!

## 🙏 Acknowledgments

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [OpenAI Whisper](https://github.com/openai/whisper)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Claude Code](https://docs.anthropic.com/)
