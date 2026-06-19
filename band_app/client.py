"""A thin REST wrapper the deterministic orchestrator uses to drive a Band room.

The orchestrator is rule code (no LLM), so it talks to Band over plain REST: it
creates the room, adds participants, posts @mention messages, and polls for the
agents' replies. This keeps control fully deterministic (plan §4, §12.6).

We use the async REST client from `band.client.rest`. Endpoint shapes verified
against band-sdk 1.0.0 by introspection (see the method calls below).
"""

from __future__ import annotations

from band.client.rest import (
    AsyncRestClient,
    ChatEventRequest,
    ChatMessageRequest,
    ChatMessageRequestMentionsItem,
    ChatRoomRequest,
    ParticipantRequest,
)

# All Band traffic goes to app.band.ai (the ws/rest host named in plan §12.6).
REST_BASE_URL = "https://app.band.ai"


class BandRoom:
    """One Band room plus the few operations the orchestrator needs on it."""

    def __init__(self, api_key: str) -> None:
        # api_key authenticates the ORCHESTRATOR agent to the platform.
        self._client = AsyncRestClient(base_url=REST_BASE_URL, api_key=api_key)
        self.room_id: str | None = None

    async def create_room(self, title: str | None = None) -> str:
        """Create a standalone room and remember its id.

        The chat-create endpoint's only field, `task_id`, is OPTIONAL and, when
        given, must reference an existing platform task — which we don't have. So
        we omit it and create a standalone room. `title` is kept for call-site
        readability; the create endpoint does not persist it.
        """
        resp = await self._client.agent_api_chats.create_agent_chat(
            chat=ChatRoomRequest(),
        )
        self.room_id = resp.data.id
        return self.room_id

    async def add_participant(self, agent_id: str, role: str = "member") -> None:
        """Add one agent (or the human) to the room by platform id."""
        await self._client.agent_api_participants.add_agent_chat_participant(
            self.room_id,
            participant=ParticipantRequest(participant_id=agent_id, role=role),
        )

    async def mention(self, content: str, agent_id: str, handle: str) -> None:
        """Post a message that @mentions exactly one agent (the next in the flow)."""
        await self._client.agent_api_messages.create_agent_chat_message(
            self.room_id,
            message=ChatMessageRequest(
                content=content,
                mentions=[ChatMessageRequestMentionsItem(id=agent_id, handle=handle)],
            ),
        )

    async def post(self, content: str) -> None:
        """Post a plain announcement (router log / scraper output) for visibility.

        Chat messages require >=1 @mention, and mentioning an agent would TRIGGER
        it — wrong for audit lines. So announcements go through the events channel
        (no mention, no trigger). Best-effort: audit posts must never break routing.
        """
        try:
            await self._client.agent_api_events.create_agent_chat_event(
                self.room_id,
                event=ChatEventRequest(content=content, message_type="thought"),
            )
        except Exception:
            pass

    async def list_messages(self) -> list:
        """Return all messages in the room (each has sender_id / sender_name / content).

        The endpoint paginates (default page_size 20 → it was silently returning
        only the newest ~10), so a long room (every Loop-B rewrite adds ~6 msgs)
        could push an agent's reply past the page and the orchestrator would never
        see it. Ask for the max page so the poll always sees the whole room.
        """
        resp = await self._client.agent_api_messages.list_agent_messages(
            self.room_id, status="all", page_size=100,
        )
        return list(resp.data or [])
