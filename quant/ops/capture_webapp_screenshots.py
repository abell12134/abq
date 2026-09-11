#!/usr/bin/env python3
"""Capture webapp tab screenshots for docs (headless Playwright)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "img"
BASE = "http://127.0.0.1:8000"
VIEWPORT = {"width": 1440, "height": 900}


async def wait_ready(page, extra_ms: int = 2500) -> None:
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(extra_ms)


async def click_tab(page, tab_id: str) -> None:
    await page.click(f'#tabs button.tab[data-tab="{tab_id}"]')
    await wait_ready(page)


async def click_board_sub(page, sub: str) -> None:
    await click_tab(page, "board")
    await page.click(f'#board-subtabs button.subtab[data-sub="{sub}"]')
    await wait_ready(page, 3000)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shots = [
        ("pic1.png", "overview", None),
        ("pic4.png", "tracking", None),
        ("pic5.png", "board", "panorama"),
        ("pic6.png", "board", "theme"),
        ("pic2.png", "sentiment", None),
        ("pic3.png", "daily-ops", None),
        ("pic7.png", "swing", None),
    ]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport=VIEWPORT)
        await page.goto(BASE, wait_until="networkidle")
        await wait_ready(page, 3500)

        for fname, tab, sub in shots:
            if tab == "board" and sub:
                await click_board_sub(page, sub)
            else:
                await click_tab(page, tab)
            path = OUT / fname
            await page.screenshot(path=str(path), full_page=False)
            print(f"wrote {path}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
