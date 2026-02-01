"""
Flask server exposing a chat endpoint for the Claude Code runner.
Use this as an alternative to the Telegram bot interface.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from threading import Thread
from typing import Optional
from uuid import uuid4

from flask import Flask, request, jsonify
from flask_cors import CORS

# Ensure imports work
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))
os.chdir(script_dir)

from config import load_settings
from claude_runner import ClaudeCodeRunner, TaskQueue, TaskStatus

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Enable CORS for cross-origin requests

# Global instances
settings = None
task_queue = None
sessions = {}  # session_id -> {"user_id": int, "agent_id": int, "model": str}


def get_or_create_session(session_id: Optional[str] = None) -> tuple[str, dict]:
    """Get existing session or create a new one."""
    if session_id and session_id in sessions:
        return session_id, sessions[session_id]
    
    # Create new session
    new_id = session_id or str(uuid4())
    # Use a unique user_id for each session (hash of session_id for consistency)
    user_id = abs(hash(new_id)) % (10**9)
    sessions[new_id] = {
        "user_id": user_id,
        "agent_id": 1,
        "model": None,  # Will use default
        "is_new": True
    }
    return new_id, sessions[new_id]


def run_async(coro):
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "claude-chat-api"
    })


@app.route('/models', methods=['GET'])
def list_models():
    """List available models."""
    return jsonify({
        "models": ClaudeCodeRunner.AVAILABLE_MODELS,
        "default": settings.claude_model if settings else "claude-sonnet-4-20250514"
    })


@app.route('/chat', methods=['POST'])
def chat():
    """
    Chat endpoint for sending messages to Claude.
    
    Request body:
    {
        "message": "Your message here",
        "session_id": "optional-session-id",
        "model": "optional-model-name",
        "new_session": false
    }
    
    Response:
    {
        "response": "Claude's response",
        "session_id": "session-id-for-continuity",
        "status": "completed",
        "execution_time": 1.23,
        "model": "model-used"
    }
    """
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
    
    message = data.get('message', '').strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400
    
    # Session management
    session_id, session = get_or_create_session(data.get('session_id'))
    
    # Model selection
    model = data.get('model')
    if model:
        # Set model for this session
        full_model = task_queue.runner.set_user_model(session['user_id'], model)
        session['model'] = full_model
    
    # Check if this should be a new session
    is_new_session = data.get('new_session', False) or session.get('is_new', False)
    
    # Clear the "is_new" flag after first message
    if session.get('is_new'):
        session['is_new'] = False
    
    logger.info(f"Chat request - session: {session_id}, message: {message[:100]}...")
    
    try:
        # Run the task
        result = run_async(
            task_queue.submit_task(
                prompt=message,
                user_id=session['user_id'],
                agent_id=session['agent_id'],
                is_new_session=is_new_session
            )
        )
        
        # Build response
        response_data = {
            "response": result.output or result.error or "No response",
            "session_id": session_id,
            "status": result.status.value,
            "execution_time": result.execution_time,
            "model": task_queue.runner.get_user_model(session['user_id'])
        }
        
        if result.status == TaskStatus.COMPLETED:
            return jsonify(response_data)
        else:
            response_data["error"] = result.error
            return jsonify(response_data), 500 if result.status == TaskStatus.FAILED else 408
            
    except Exception as e:
        logger.error(f"Chat error: {e}")
        return jsonify({
            "error": str(e),
            "session_id": session_id
        }), 500


@app.route('/session/<session_id>', methods=['DELETE'])
def delete_session(session_id: str):
    """Delete a session to start fresh."""
    if session_id in sessions:
        del sessions[session_id]
        return jsonify({"message": f"Session {session_id} deleted"})
    return jsonify({"error": "Session not found"}), 404


@app.route('/session/<session_id>/model', methods=['PUT'])
def set_session_model(session_id: str):
    """Set the model for a session."""
    if session_id not in sessions:
        return jsonify({"error": "Session not found"}), 404
    
    data = request.get_json()
    model = data.get('model')
    if not model:
        return jsonify({"error": "Model is required"}), 400
    
    session = sessions[session_id]
    full_model = task_queue.runner.set_user_model(session['user_id'], model)
    session['model'] = full_model
    
    return jsonify({
        "session_id": session_id,
        "model": full_model
    })


def initialize():
    """Initialize settings and task queue."""
    global settings, task_queue
    
    try:
        settings = load_settings()
    except Exception as e:
        logger.warning(f"Could not load .env settings: {e}")
        # Use defaults
        from dataclasses import dataclass
        
        @dataclass
        class DefaultSettings:
            working_directory = Path.home()
            claude_code_path = None
            task_timeout = 300
            claude_model = "claude-sonnet-4-20250514"
        
        settings = DefaultSettings()
    
    runner = ClaudeCodeRunner(
        working_dir=settings.working_directory,
        claude_path=settings.claude_code_path,
        timeout=settings.task_timeout,
        default_model=settings.claude_model
    )
    task_queue = TaskQueue(runner)
    
    logger.info(f"Initialized with working dir: {settings.working_directory}")
    logger.info(f"Default model: {settings.claude_model}")


def main(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """Run the Flask server."""
    initialize()
    
    print(f"""
============================================================
        Claude Code Chat API
============================================================
  Server running at: http://{host}:{port}

  Endpoints:
    POST /chat          - Send a message
    GET  /models        - List available models
    GET  /health        - Health check
    DELETE /session/id  - Delete a session
    PUT  /session/id/model - Set session model

  Example usage:
    curl -X POST http://localhost:{port}/chat \\
      -H "Content-Type: application/json" \\
      -d '{{"message": "Hello, write a hello world in Python"}}'

  Press Ctrl+C to stop
============================================================
    """)
    
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Claude Code Chat API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    
    args = parser.parse_args()
    main(host=args.host, port=args.port, debug=args.debug)
