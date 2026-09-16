"""
Interactive Telegram Bot Listener for Human-in-the-Loop Question Training.

Design Decisions:
- Direct Telegram Replies: Allows the user to reply to a Telegram question alert on their phone.
- Auto-Extraction: Parses `[#<id>]` from reply headers or `#<id> <answer>` format.
- Instant Resolution: Calls `repo.resolve_review()` to update `question_review` and insert into `answer_kb`.
"""

from __future__ import annotations

import asyncio
import html
import re

import httpx

from ..db.repository import Repository
from ..logging_setup import get_logger

log = get_logger(__name__)


class TelegramListener:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        # Token stays out of instance state that could be logged: URLs are
        # built per request from the raw token.
        self._bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.offset = 0

    def _endpoint(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._bot_token}/{method}"

    async def send_confirmation(self, text: str) -> None:
        """Sends a confirmation reply back to the Telegram chat."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    self._endpoint("sendMessage"),
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                )
        except Exception as exc:
            redacted = str(exc).replace(self._bot_token, "[redacted]") if self._bot_token else str(exc)
            log.warning("telegram_listener.send_error", error=redacted[:150])

    async def poll_once(self, repo: Repository) -> int:
        """
        Polls Telegram getUpdates endpoint once and processes any pending replies.
        Returns the number of resolved questions.
        """
        if not self._bot_token or not self.chat_id:
            return 0

        resolved_count = 0
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    self._endpoint("getUpdates"),
                    params={"offset": self.offset, "timeout": 2},
                )
                if resp.status_code != 200:
                    return 0

                data = resp.json()
                if not data.get("ok"):
                    return 0

                updates = data.get("result", [])
                for update in updates:
                    self.offset = max(self.offset, update["update_id"] + 1)
                    msg = update.get("message")
                    if not msg:
                        continue

                    # Verify message came from configured chat_id
                    msg_chat_id = str(msg.get("chat", {}).get("id", ""))
                    if msg_chat_id != self.chat_id:
                        continue

                    user_text = (msg.get("text") or "").strip()
                    if not user_text:
                        continue

                    review_id: int | None = None
                    answer_text: str = ""

                    # Case A: User replied directly to a bot message containing [#123]
                    reply_to = msg.get("reply_to_message", {})
                    if reply_to:
                        reply_text = reply_to.get("text") or ""
                        match = re.search(r"\[#(\d+)\]", reply_text)
                        if match:
                            review_id = int(match.group(1))
                            answer_text = user_text

                    # Case B: User typed `#123 Yes` directly
                    if review_id is None:
                        match = re.match(r"^#(\d+)\s+(.+)$", user_text, re.DOTALL)
                        if match:
                            review_id = int(match.group(1))
                            answer_text = match.group(2).strip()

                    if review_id is not None and answer_text:
                        success = await repo.resolve_review(review_id, answer_text)
                        if success:
                            resolved_count += 1
                            log.info("telegram_listener.question_resolved", review_id=review_id, answer_chars=len(answer_text))
                            await self.send_confirmation(
                                f"✅ <b>Question [#{review_id}] Resolved!</b>\n"
                                f"<b>Answer:</b> <code>{html.escape(answer_text)}</code>\n"
                                f"Saved to Answer KB & self-learning engine."
                            )
                        else:
                            await self.send_confirmation(
                                f"⚠️ Could not resolve question [#{review_id}]. It may already be resolved."
                            )

        except Exception as exc:
            log.warning("telegram_listener.poll_error", error=str(exc)[:150])

        return resolved_count

    async def start_listening_loop(self, repo: Repository, poll_interval_s: float = 3.0) -> None:
        """Runs a continuous polling loop to process replies from Telegram."""
        log.info("telegram_listener.started", chat_id=self.chat_id)
        await self.send_confirmation("🤖 <b>Telegram Question Listener is ACTIVE!</b>\nReply to any question alert to train your AI.")

        try:
            while True:
                await self.poll_once(repo)
                await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            log.info("telegram_listener.stopped")
