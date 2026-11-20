"""Async persistence for the management endpoint Playground.

These records are a UI transcript. They never read, rewrite, or delete Strands
session snapshots and long-term memories owned by configured Agents.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.schema import (
    PlaygroundConversationRecord,
    PlaygroundMessageRecord,
)


class PlaygroundRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self.database = database

    async def create_conversation(
        self,
        *,
        workspace_id: str,
        endpoint_id: str,
        endpoint_name: str,
        user_id: str,
        generation: int,
    ) -> PlaygroundConversationRecord:
        record = PlaygroundConversationRecord(
            id=f"conversation_{uuid.uuid4().hex}",
            workspace_id=workspace_id,
            endpoint_id=endpoint_id,
            endpoint_name=endpoint_name,
            user_id=user_id,
            thread_id=f"playground_{uuid.uuid4().hex}",
            created_generation=generation,
        )
        async with self.database.session() as session, session.begin():
            session.add(record)
        return record

    async def list_conversations(
        self,
        workspace_id: str,
        *,
        limit: int = 200,
    ) -> list[PlaygroundConversationRecord]:
        statement = (
            select(PlaygroundConversationRecord)
            .where(PlaygroundConversationRecord.workspace_id == workspace_id)
            .order_by(
                PlaygroundConversationRecord.updated_at.desc(),
                PlaygroundConversationRecord.id.desc(),
            )
            .limit(max(1, min(limit, 500)))
        )
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def get_conversation(
        self,
        workspace_id: str,
        conversation_id: str,
    ) -> PlaygroundConversationRecord | None:
        statement = select(PlaygroundConversationRecord).where(
            PlaygroundConversationRecord.id == conversation_id,
            PlaygroundConversationRecord.workspace_id == workspace_id,
        )
        async with self.database.session() as session:
            return await session.scalar(statement)

    async def list_messages(
        self,
        workspace_id: str,
        conversation_id: str,
    ) -> list[PlaygroundMessageRecord]:
        statement = (
            select(PlaygroundMessageRecord)
            .where(
                PlaygroundMessageRecord.workspace_id == workspace_id,
                PlaygroundMessageRecord.conversation_id == conversation_id,
            )
            .order_by(PlaygroundMessageRecord.sequence)
        )
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def append_messages(
        self,
        *,
        workspace_id: str,
        conversation_id: str,
        messages: Sequence[dict[str, Any]],
    ) -> bool:
        """Append display entries atomically and update the conversation summary.

        Returns ``False`` when the conversation is missing or a stable message ID
        already exists. The latter makes accidental client retries explicit instead
        of invoking the Agent twice.
        """

        if not messages:
            return True
        now = datetime.now(UTC)
        try:
            async with self.database.session() as session, session.begin():
                conversation = await session.scalar(
                    select(PlaygroundConversationRecord)
                    .where(
                        PlaygroundConversationRecord.id == conversation_id,
                        PlaygroundConversationRecord.workspace_id == workspace_id,
                    )
                    .with_for_update()
                )
                if conversation is None:
                    return False
                ids = [str(item["id"]) for item in messages]
                duplicate = await session.scalar(
                    select(PlaygroundMessageRecord.sequence).where(
                        PlaygroundMessageRecord.conversation_id == conversation_id,
                        PlaygroundMessageRecord.id.in_(ids),
                    )
                )
                if duplicate is not None:
                    return False
                records = [
                    PlaygroundMessageRecord(
                        id=str(item["id"]),
                        workspace_id=workspace_id,
                        conversation_id=conversation_id,
                        run_id=item.get("run_id"),
                        kind=str(item.get("kind") or "message"),
                        role=item.get("role"),
                        content=str(item.get("content") or ""),
                        payload=dict(item.get("payload") or {}),
                        created_at=item.get("created_at") or now,
                    )
                    for item in messages
                ]
                session.add_all(records)
                conversation.message_count += len(records)
                conversation.updated_at = now
                preview = next(
                    (
                        record.content.strip()
                        for record in reversed(records)
                        if record.content.strip()
                    ),
                    "",
                )
                if preview:
                    conversation.last_message_preview = " ".join(preview.split())[:512]
                if not conversation.title:
                    first_user = next(
                        (
                            record.content.strip()
                            for record in records
                            if record.kind == "message"
                            and record.role == "user"
                            and record.content.strip()
                        ),
                        "",
                    )
                    if first_user:
                        conversation.title = " ".join(first_user.split())[:80]
            return True
        except IntegrityError:
            return False

    async def delete_conversation(self, workspace_id: str, conversation_id: str) -> bool:
        async with self.database.session() as session, session.begin():
            conversation = await session.scalar(
                select(PlaygroundConversationRecord.id).where(
                    PlaygroundConversationRecord.id == conversation_id,
                    PlaygroundConversationRecord.workspace_id == workspace_id,
                )
            )
            if conversation is None:
                return False
            await session.execute(
                delete(PlaygroundMessageRecord).where(
                    PlaygroundMessageRecord.conversation_id == conversation_id,
                    PlaygroundMessageRecord.workspace_id == workspace_id,
                )
            )
            await session.execute(
                delete(PlaygroundConversationRecord).where(
                    PlaygroundConversationRecord.id == conversation_id,
                    PlaygroundConversationRecord.workspace_id == workspace_id,
                )
            )
            return True
