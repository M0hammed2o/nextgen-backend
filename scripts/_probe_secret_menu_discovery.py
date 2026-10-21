"""
One-off, read-only probe: does the REAL unmodified bot pipeline's LLM path
(prompt_builder.build_system_prompt + llm/provider.py's OpenAIProvider)
let a customer discover the Mochachos secret menu and specials by asking,
using the actual seeded Mochachos demo business data?

Not a pytest test. Run manually, never as part of CI. Makes real OpenAI
API calls (small cost). Does not touch the protected pipeline files --
only imports and calls them exactly as routes_webhook.py already does.
"""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://nextgen_test:nextgen_test_only_do_not_use_in_prod@localhost:55677/nextgen_test")


async def main() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from backend.app.bot.prompt_builder import build_system_prompt
    from backend.app.core.config import get_settings
    from backend.app.llm.provider import OpenAIProvider
    from shared.models import Business, MenuCategory, MenuItem
    from shared.models.specials import Special

    engine = create_async_engine(os.environ["DATABASE_URL"])
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        business = (await db.execute(
            select(Business).where(Business.slug == "mochachos-nextgen-ai-demo")
        )).scalar_one()
        categories = list((await db.execute(
            select(MenuCategory).where(MenuCategory.business_id == business.id)
        )).scalars().all())
        menu_items = list((await db.execute(
            select(MenuItem).where(MenuItem.business_id == business.id, MenuItem.is_active == True)  # noqa: E712
        )).scalars().all())
        specials = list((await db.execute(
            select(Special).where(Special.business_id == business.id, Special.is_active == True)  # noqa: E712
        )).scalars().all())

    await engine.dispose()

    system_prompt = build_system_prompt(
        business=business, categories=categories, menu_items=menu_items, specials=specials,
        conversation_state="BROWSING", cart=[],
    )
    print(f"--- system prompt length: {len(system_prompt)} chars ---\n")

    settings = get_settings()
    provider = OpenAIProvider(api_key=settings.OPENAI_API_KEY, model=settings.LLM_DEFAULT_MODEL)

    questions = [
        "Do you have a secret menu?",
        "What exclusive meals do you have?",
        "Do you have anything that isn't on the normal menu?",
        "What specials do you recommend?",
        "What time do you close, and do you deliver?",
    ]

    for q in questions:
        resp = await provider.complete(system_prompt=system_prompt, user_message=q, max_tokens=400)
        print(f"CUSTOMER: {q}")
        print(f"BOT: {resp.text}\n")
        print(f"  (tokens: {resp.input_tokens}+{resp.output_tokens}, cost_cents={resp.cost_cents})\n")


if __name__ == "__main__":
    asyncio.run(main())
