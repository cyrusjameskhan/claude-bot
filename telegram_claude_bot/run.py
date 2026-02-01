#!/usr/bin/env python3
"""
Simple runner script for the Telegram Claude Bot.
Use this to start the bot from the command line.
"""

import sys
from pathlib import Path

# Ensure we're in the right directory
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

# Change to script directory for relative imports
import os
os.chdir(script_dir)

if __name__ == "__main__":
    print("""
============================================================
        Claude Code Telegram Bot
============================================================
  Starting server...

  Once running:
  1. Open your bot in Telegram
  2. Send /pair YOUR_SECRET to authenticate
  3. Send messages or voice notes!

  Press Ctrl+C to stop
============================================================
    """)
    
    from bot import main
    main()
