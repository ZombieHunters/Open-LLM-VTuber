from datetime import datetime
import uuid
import json
import asyncio
from typing import AsyncIterator, List
import numpy as np
from loguru import logger
from fastapi import WebSocket

from .live2d_model import Live2dModel
from .asr.asr_interface import ASRInterface
from .agent.agents.agent_interface import AgentInterface
from .agent.output_types import AgentOutputBase, SentenceOutput, AudioOutput, Actions
from .tts.tts_interface import TTSInterface

from .utils.stream_audio import prepare_audio_payload
from .chat_history_manager import store_message


class TTSTaskManager:
    """Manages TTS tasks and their sequential execution"""

    def __init__(self):
        self.task_list: List[asyncio.Task] = []
        self.next_index_to_play: int = 0

    def clear(self):
        """Clear all tasks and reset counter"""
        self.task_list.clear()
        self.next_index_to_play = 0

    async def speak(
        self,
        tts_text: str,
        live2d_model: Live2dModel,
        tts_engine: TTSInterface,
        websocket_send: WebSocket.send,
        display_text: str | None = None,
        actions: Actions | None = None,
    ) -> None:
        """
        Generate and send audio for a sentence

        Args:
            tts_text: Text to be spoken
            live2d_model: Live2D model instance
            tts_engine: TTS engine instance
            websocket_send: WebSocket send function
            display_text: Text to display (defaults to tts_text)
            actions: Actions object
        """
        if not display_text:
            display_text = tts_text

        if not tts_text or not tts_text.strip():
            logger.error(
                f'TTS receives "{tts_text}", which is empty. Nothing to speak.'
            )
            return

        logger.debug(f"🏃Generating audio for '''{tts_text}'''...")

        current_task_index = len(self.task_list)
        tts_task = asyncio.create_task(
            tts_engine.async_generate_audio(
                text=tts_text,
                file_name_no_ext=f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}",
            )
        )
        self.task_list.append(tts_task)

        try:
            while current_task_index != self.next_index_to_play:
                await asyncio.sleep(0.01)

            audio_file_path = await tts_task

            try:
                audio_payload = prepare_audio_payload(
                    audio_path=audio_file_path,
                    actions=actions,
                    display_text=display_text,
                )
                logger.debug("Sending Audio payload.")
                await websocket_send(json.dumps(audio_payload))

                tts_engine.remove_file(audio_file_path)
                logger.debug("Payload sent. Audio cache file cleaned.")

            except Exception as e:
                logger.error(f"Error preparing audio payload: {e}")
                tts_engine.remove_file(audio_file_path)

        except Exception as e:
            logger.error(f"Error in speak function: {e}")
        finally:
            self.next_index_to_play += 1

            if current_task_index == len(self.task_list) - 1:
                self.clear()


async def conversation_chain(
    user_input: str | np.ndarray,
    asr_engine: ASRInterface,
    agent_engine: AgentInterface,
    tts_engine: TTSInterface,
    live2d_model: Live2dModel,
    websocket_send: WebSocket.send,
    conf_uid: str = "",
    history_uid: str = "",
) -> str:
    """
    One iteration of the main conversation chain.

    Args:
        user_input: User input (string or audio array)
        asr_engine: ASR engine instance
        agent_engine: Agent instance
        tts_engine: TTS engine instance
        live2d_model: Live2D model instance
        tts_preprocessor_config: TTS preprocessor config
        translate_engine: Optional translator instance
        websocket_send: WebSocket send function
        conf_uid: Configuration ID
        history_uid: History ID

    Returns:
        str: Complete response from the agent
    """
    tts_manager = TTSTaskManager()
    full_response: str = ""

    try:
        session_emoji = np.random.choice(EMOJI_LIST)

        await websocket_send(
            json.dumps(
                {
                    "type": "control",
                    "text": "conversation-chain-start",
                }
            )
        )

        logger.info(f"New Conversation Chain {session_emoji} started!")

        # Handle audio input
        if isinstance(user_input, np.ndarray):
            logger.info("Transcribing audio input...")
            user_input = await asr_engine.async_transcribe_np(user_input)
            await websocket_send(
                json.dumps({"type": "user-input-transcription", "text": user_input})
            )

        store_message(conf_uid, history_uid, "human", user_input)
        logger.info(f"User input: {user_input}")

        # Process agent output
        agent_output: AsyncIterator[AgentOutputBase] = agent_engine.chat(user_input)

        async for output in agent_output:
            if isinstance(output, SentenceOutput):
                async for display_text, tts_text, actions in output:
                    full_response += display_text
                    await tts_manager.speak(
                        tts_text=tts_text,
                        display_text=display_text,
                        actions=actions,
                        live2d_model=live2d_model,
                        tts_engine=tts_engine,
                        websocket_send=websocket_send,
                    )
            elif isinstance(output, AudioOutput):
                async for audio_path, display_text, transcript, actions in output:
                    full_response += display_text
                    audio_payload = prepare_audio_payload(
                        audio_path=audio_path,
                        display_text=display_text,
                        actions=actions,
                    )
                    await websocket_send(json.dumps(audio_payload))

        if tts_manager.task_list:
            await asyncio.gather(*tts_manager.task_list)

    except asyncio.CancelledError:
        logger.info(f"🤡👍 Conversation {session_emoji} cancelled because interrupted.")

    finally:
        logger.debug(f"🧹 Clearing up conversation {session_emoji}.")
        tts_manager.clear()

        if full_response:
            store_message(conf_uid, history_uid, "ai", full_response)
            logger.info(f"💾 Stored AI message: '''{full_response}'''")

        await websocket_send(
            json.dumps(
                {
                    "type": "control",
                    "text": "conversation-chain-end",
                }
            )
        )
        logger.info(f"😎👍✅ Conversation Chain {session_emoji} completed!")
        return full_response


EMOJI_LIST = [
    "🐶",
    "🐱",
    "🐭",
    "🐹",
    "🐰",
    "🦊",
    "🐻",
    "🐼",
    "🐨",
    "🐯",
    "🦁",
    "🐮",
    "🐷",
    "🐸",
    "🐵",
    "🐔",
    "🐧",
    "🐦",
    "🐤",
    "🐣",
    "🐥",
    "🦆",
    "🦅",
    "🦉",
    "🦇",
    "🐺",
    "🐗",
    "🐴",
    "🦄",
    "🐝",
    "🌵",
    "🎄",
    "🌲",
    "🌳",
    "🌴",
    "🌱",
    "🌿",
    "☘️",
    "🍀",
    "🍂",
    "🍁",
    "🍄",
    "🌾",
    "💐",
    "🌹",
    "🌸",
    "🌛",
    "🌍",
    "⭐️",
    "🔥",
    "🌈",
    "🌩",
    "⛄️",
    "🎃",
    "🎄",
    "🎉",
    "🎏",
    "🎗",
    "🀄️",
    "🎭",
    "🎨",
    "🧵",
    "🪡",
    "🧶",
    "🥽",
    "🥼",
    "🦺",
    "👔",
    "👕",
    "👜",
    "👑",
]
