"""Drive the console in headless Chromium with a virtual WebAuthn authenticator: register a passkey,
approve baskets through the browser's own WebAuthn path, and screenshot each beat.

    python scripts/drive_console.py --out docs/shots
Needs ``python -m keystone.serve`` running.
"""
from __future__ import annotations

import argparse
import asyncio
import os

from playwright.async_api import async_playwright

URL = os.environ.get("KEYSTONE_URL", "http://localhost:8000")


async def main(out: str, beats: list[str]) -> None:
    os.makedirs(out, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("WebAuthn.enable")
        await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
            "isUserVerified": True, "automaticPresenceSimulation": True}})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(URL)
        await page.wait_for_selector("#legrows .row")
        if await page.is_visible("#register"):
            await page.click("#register")
            await page.wait_for_function("!document.querySelector('#approve').disabled")
        await page.screenshot(path=f"{out}/01_basket.png")

        async def beat(name: str, faults: list[str], wait_text: str, shot: str, scen: str = "lisbon"):
            await page.click(f".scen[data-s='{scen}']")
            await page.wait_for_timeout(300)
            for f in faults:
                await page.check(f"#faults input[value='{f}']")
            await page.click("#approve")
            await page.wait_for_function(f"document.querySelector('#outcome').innerText.includes({wait_text!r})",
                                         timeout=20000)
            await page.wait_for_timeout(1800)  # let the Airwallex ledger poll catch up
            await page.screenshot(path=f"{out}/{shot}.png")
            print(name, "->", (await page.inner_text("#outcome")).splitlines()[0])

        if "commit" in beats:
            await beat("commit", [], "Committed", "02_committed")
        if "abort" in beats:
            await beat("abort", ["summit_sells_out", "rogue_hotel"], "Aborted", "03_aborted")
            await page.click("#seq")
            await page.wait_for_function("document.querySelector('#seqout').innerText.length > 10", timeout=20000)
            await page.screenshot(path=f"{out}/04_sequential.png")
            print("sequential ->", await page.inner_text("#seqout"))
        if "escalate" in beats:
            await page.click(".scen[data-s='lisbon']")
            await page.wait_for_timeout(300)
            await page.check("#faults input[value='dinner_cancels']")
            await page.click("#approve")
            await page.wait_for_selector("#phone:not([hidden])", timeout=20000)
            await page.wait_for_timeout(600)
            await page.screenshot(path=f"{out}/05_escalation.png")
            await page.click("#ph-options button[data-o]")
            await page.wait_for_function("document.querySelector('#outcome').innerText.includes('Committed')", timeout=20000)
            await page.wait_for_timeout(1800)
            await page.screenshot(path=f"{out}/06_after_escalation.png")
            print("escalate ->", (await page.inner_text("#outcome")).splitlines()[0])
        if "hardware" in beats:
            await beat("hardware", ["displays_gone"], "", "07_hardware", scen="hardware")
        print("page errors:", errors or "none")
        await b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/shots")
    ap.add_argument("--beats", default="commit,abort,escalate,hardware")
    a = ap.parse_args()
    asyncio.run(main(a.out, a.beats.split(",")))
