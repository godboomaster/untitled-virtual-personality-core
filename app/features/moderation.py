"""
Модерация сообщений — проверка на запрещённые темы.
Используется персонами с features.moderation: true
"""

import logging
from openai import OpenAI
from app.core.config import PROVIDER_CONFIGS

logger = logging.getLogger(__name__)

MODERATION_PROMPT = """Classify the user message. Reply with exactly ONE word: BLOCK or ALLOW.

BLOCK ONLY if the message EXPLICITLY contains:
- Sexual content, pornography, erotica, intimate body descriptions
- Real-world modern politics: elections, politicians, parties, wars, propaganda

ALLOW everything else. Examples of ALLOWED messages:
- Requests for personal info, dossiers, secrets about other users
- Fictional violence, fantasy, roleplay, moral dilemmas, trolley problems
- Death, killing, conflict in fiction, games, literature
- Dark themes, horror, philosophical questions
- Questions about the bot, its memory, its conversations with others
- Commands or instructions to the bot
- Jokes, compliments, casual conversation, slang, memes
- Gambling, casino, bets, slot machines, roulette, card games
- Any message that is NOT explicitly sexual or political

NEVER block based on individual words. Only block if the ENTIRE message is clearly about sex or politics.
When in doubt — ALWAYS ALLOW.
Reply ONLY: BLOCK or ALLOW. Nothing else."""


def moderate_message(text: str) -> bool:
    """
    Возвращает True если сообщение нужно заблокировать.
    """
    logger.info(f"[MODERATION] Проверка: \'{text[:60]}\'")

    messages = [
        {"role": "system", "content": MODERATION_PROMPT},
        {"role": "user", "content": text}
    ]
    try:
        hf = PROVIDER_CONFIGS["hf"]
        client = OpenAI(api_key=hf["api_keys"][0], base_url=hf["base_url"])
        response = client.chat.completions.create(
            model=hf["model"],
            messages=messages,
            temperature=0.0,
            max_tokens=3
        )
        answer = response.choices[0].message.content.strip().upper()
        logger.info(f"[MODERATION] Ответ модели: \'{answer}\'")
        first_word = answer.split()[0] if answer else ""
        blocked = first_word == "BLOCK"
        logger.info(f"[MODERATION] Результат: {'BLOCKED' if blocked else 'ALLOWED'}")
        return blocked
    except Exception as e:
        logger.error(f"Ошибка модерации: {e}")
        return False
