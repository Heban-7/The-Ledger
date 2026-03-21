"""
ledger/domain/aggregates/agent_session.py

AgentSession aggregate — Gas Town pattern: context must be loaded before decisions.
Stream: agent-{agent_type}-{session_id}
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ledger.exceptions import DomainError


@dataclass
class AgentSessionAggregate:
    agent_type: str
    session_id: str
    agent_id: str = ""
    context_loaded: bool = False
    model_version: str | None = None
    version: int = -1
    events: list[dict] = field(default_factory=list)

    @property
    def stream_id(self) -> str:
        return f"agent-{self.agent_type}-{self.session_id}"

    @classmethod
    async def load(cls, store, agent_type: str, session_id: str) -> "AgentSessionAggregate":
        stream_id = f"agent-{agent_type}-{session_id}"
        events = await store.load_stream(stream_id)
        agg = cls(agent_type=agent_type, session_id=session_id, agent_id=f"{agent_type}-agent")
        for event in events:
            agg.apply(event)
        return agg

    def apply(self, event: dict) -> None:
        et = event.get("event_type")
        p = event.get("payload", {})
        self.version = event.get("stream_position", self.version + 1)
        self.events.append(event)

        if et == "AgentSessionStarted":
            self.context_loaded = True
            self.model_version = p.get("model_version")
        elif et == "AgentSessionCompleted":
            pass
        elif et == "AgentSessionFailed":
            pass

    def assert_context_loaded(self) -> None:
        if not self.context_loaded:
            raise DomainError(
                "AgentSession must have AgentSessionStarted before any decision event (Gas Town pattern)",
                rule="context_loaded",
            )

    def assert_model_version_current(self, model_version: str) -> None:
        if self.model_version and self.model_version != model_version:
            raise DomainError(
                f"Model version mismatch: session has {self.model_version}, got {model_version}",
                rule="model_version",
            )

    def assert_session_not_started(self) -> None:
        """Duplicate AgentSessionStarted is not allowed for the same stream."""
        if self.version >= 0:
            raise DomainError(f"Session {self.session_id} already started", rule="duplicate_session")
