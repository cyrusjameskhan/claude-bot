"""
Multi-agent session manager.
Tracks multiple Claude Code sessions per user.
"""

import json
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class AgentSession:
    """Represents a single Claude Code agent session."""
    agent_id: int
    session_id: str
    user_id: int
    name: str
    created_at: str
    last_used: str
    message_count: int = 0
    is_active: bool = True
    
    def touch(self):
        """Update last used timestamp."""
        self.last_used = datetime.now().isoformat()
        self.message_count += 1


@dataclass 
class UserAgents:
    """All agents for a single user."""
    user_id: int
    agents: Dict[int, AgentSession] = field(default_factory=dict)
    default_agent: int = 1
    
    def get_agent(self, agent_id: int) -> Optional[AgentSession]:
        """Get agent by ID."""
        return self.agents.get(agent_id)
    
    def get_or_create_agent(self, agent_id: int) -> AgentSession:
        """Get existing agent or create new one."""
        if agent_id not in self.agents:
            now = datetime.now().isoformat()
            self.agents[agent_id] = AgentSession(
                agent_id=agent_id,
                session_id=str(uuid.uuid4()),  # Full UUID for Claude Code --resume
                user_id=self.user_id,
                name=f"Agent {agent_id}" if agent_id > 1 else "Main Agent",
                created_at=now,
                last_used=now,
            )
            logger.info(f"Created new agent {agent_id} for user {self.user_id}")
        return self.agents[agent_id]
    
    def terminate_agent(self, agent_id: int) -> bool:
        """Terminate an agent session."""
        if agent_id in self.agents:
            self.agents[agent_id].is_active = False
            del self.agents[agent_id]
            logger.info(f"Terminated agent {agent_id} for user {self.user_id}")
            return True
        return False
    
    def get_active_agents(self) -> List[AgentSession]:
        """Get all active agents."""
        return [a for a in self.agents.values() if a.is_active]
    
    def get_next_agent_id(self) -> int:
        """Get the next available agent ID."""
        if not self.agents:
            return 1
        return max(self.agents.keys()) + 1


class AgentManager:
    """
    Manages multiple Claude Code agent sessions across all users.
    Persists session data to disk.
    """
    
    MAX_AGENTS_PER_USER = 5
    
    def __init__(self, data_dir: Path = None):
        self.data_dir = data_dir or Path("data")
        self.data_dir.mkdir(exist_ok=True)
        self._sessions_file = self.data_dir / "agent_sessions.json"
        self._user_agents: Dict[int, UserAgents] = {}
        self._load_sessions()
    
    def _load_sessions(self) -> None:
        """Load sessions from disk."""
        if self._sessions_file.exists():
            try:
                data = json.loads(self._sessions_file.read_text())
                for user_id_str, user_data in data.items():
                    user_id = int(user_id_str)
                    agents = {}
                    for agent_id_str, agent_data in user_data.get("agents", {}).items():
                        agent_id = int(agent_id_str)
                        agents[agent_id] = AgentSession(**agent_data)
                    self._user_agents[user_id] = UserAgents(
                        user_id=user_id,
                        agents=agents,
                        default_agent=user_data.get("default_agent", 1)
                    )
                logger.info(f"Loaded sessions for {len(self._user_agents)} users")
            except Exception as e:
                logger.error(f"Failed to load sessions: {e}")
                self._user_agents = {}
    
    def _save_sessions(self) -> None:
        """Save sessions to disk."""
        data = {}
        for user_id, user_agents in self._user_agents.items():
            data[str(user_id)] = {
                "user_id": user_id,
                "default_agent": user_agents.default_agent,
                "agents": {
                    str(aid): asdict(agent) 
                    for aid, agent in user_agents.agents.items()
                }
            }
        self._sessions_file.write_text(json.dumps(data, indent=2))
    
    def get_user_agents(self, user_id: int) -> UserAgents:
        """Get or create user agents container."""
        if user_id not in self._user_agents:
            self._user_agents[user_id] = UserAgents(user_id=user_id)
        return self._user_agents[user_id]
    
    def get_agent(self, user_id: int, agent_id: int) -> Optional[AgentSession]:
        """Get a specific agent for a user."""
        user_agents = self.get_user_agents(user_id)
        return user_agents.get_agent(agent_id)
    
    def get_or_create_agent(self, user_id: int, agent_id: int) -> tuple[AgentSession, bool]:
        """
        Get or create an agent.
        Returns (agent, was_created).
        """
        user_agents = self.get_user_agents(user_id)
        
        # Check limit
        if agent_id not in user_agents.agents:
            if len(user_agents.get_active_agents()) >= self.MAX_AGENTS_PER_USER:
                raise ValueError(f"Maximum {self.MAX_AGENTS_PER_USER} agents allowed. Use /terminate to close one.")
        
        was_new = agent_id not in user_agents.agents
        agent = user_agents.get_or_create_agent(agent_id)
        self._save_sessions()
        return agent, was_new
    
    def terminate_agent(self, user_id: int, agent_id: int) -> bool:
        """Terminate an agent."""
        user_agents = self.get_user_agents(user_id)
        result = user_agents.terminate_agent(agent_id)
        if result:
            self._save_sessions()
        return result
    
    def create_new_agent(self, user_id: int) -> AgentSession:
        """Create a new agent with the next available ID."""
        user_agents = self.get_user_agents(user_id)
        
        if len(user_agents.get_active_agents()) >= self.MAX_AGENTS_PER_USER:
            raise ValueError(f"Maximum {self.MAX_AGENTS_PER_USER} agents allowed.")
        
        next_id = user_agents.get_next_agent_id()
        agent, _ = self.get_or_create_agent(user_id, next_id)
        return agent
    
    def get_active_agents(self, user_id: int) -> List[AgentSession]:
        """Get all active agents for a user."""
        user_agents = self.get_user_agents(user_id)
        return user_agents.get_active_agents()
    
    def touch_agent(self, user_id: int, agent_id: int) -> None:
        """Update agent's last used time."""
        agent = self.get_agent(user_id, agent_id)
        if agent:
            agent.touch()
            self._save_sessions()


def parse_agent_command(message: str) -> tuple[Optional[int], str]:
    """
    Parse message for agent routing.
    
    Examples:
        "/2 hello" -> (2, "hello")
        "/3 check logs" -> (3, "check logs")  
        "hello" -> (None, "hello") -> uses default agent
        "/agents" -> (None, "/agents") -> command, not agent
    
    Returns (agent_id, remaining_message).
    """
    message = message.strip()
    
    # Check for /N pattern at start (where N is a number)
    if message.startswith("/"):
        parts = message[1:].split(None, 1)
        if parts:
            first = parts[0]
            # Check if it's a number (agent ID)
            if first.isdigit():
                agent_id = int(first)
                content = parts[1] if len(parts) > 1 else ""
                return agent_id, content
    
    # No agent specified, use default
    return None, message
