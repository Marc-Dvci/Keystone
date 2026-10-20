"""The agent's browser for merchant checkouts that collect the card in Airwallex's Card Element.

Server-to-server card entry ("Native API") needs a PCI-DSS report on compliance, so in live mode a
merchant creates the payment intent on its server and serves a checkout page with Airwallex.js; the
agent opens it in a headless browser and types the leg's credential into Airwallex's own iframe, with
``autoCapture: false`` (a hold). Every request the browser makes to the merchant is TAP-signed with
``agent-payer-auth``, the way Visa's Trusted Agent Protocol describes agent browsers; requests to
Airwallex's domains carry no agent headers.
"""
from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlsplit

from . import tap
from .airwallex.base import CardDetails


def page_html(intent_id: str, client_secret: str, currency: str, env: str, pre_auth: bool, title: str,
              amount: str) -> str:
    opts = "authorizationType: 'pre_auth'" if pre_auth else "autoCapture: false"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Checkout</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://static.airwallex.com/components/sdk/v1/index.js"></script>
<style>body{{font:15px system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;color:#0b0d12;background:#fff}}
.bar{{padding:18px 32px;border-bottom:1px solid #e6e8ec;font-weight:600}} main{{padding:24px 32px;max-width:560px}}
.amt{{font-size:28px;font-weight:600;margin:6px 0 4px}} .note{{color:#667085;margin:0 0 18px}}
#card{{border:1px solid #d0d5dd;border-radius:8px;padding:12px;margin:0 0 16px}}
#pay{{background:#0b0d12;color:#fff;border:0;border-radius:6px;padding:10px 18px;font-weight:600;font-size:15px}}
#out{{display:none}}</style>
</head><body><div class="bar">{title}</div><main><div class="amt">{amount} {currency}</div>
<p class="note">Authorization hold only. Captured when every leg of the basket is held; voided free otherwise.</p>
<div id="card"></div><button id="pay">Place hold</button><pre id="out"></pre></main>
<script>
(async () => {{
  try {{
    await window.AirwallexComponentsSDK.init({{ env: '{env}', enabledElements: ['payments'] }});
    const card = await window.AirwallexComponentsSDK.createElement('card',
      {{ intent_id: '{intent_id}', client_secret: '{client_secret}', currency: '{currency}', {opts} }});
    card.mount('card');
    document.getElementById('pay').onclick = () => card.confirm({{ intent_id: '{intent_id}', client_secret: '{client_secret}' }})
      .then(r => {{ document.getElementById('out').textContent = JSON.stringify({{status: r.status, attempt: r.latest_payment_attempt && r.latest_payment_attempt.status}}); document.title = 'held'; }})
      .catch(e => {{ document.getElementById('out').textContent = JSON.stringify(e); document.title = 'failed'; }});
    document.title = 'ready';
  }} catch (e) {{ document.title = 'failed'; document.getElementById('out').textContent = String(e); }}
}})();
</script></body></html>"""


class AgentBrowser:
    """One headless Chromium for the agent, reused across legs."""

    def __init__(self, key: tap.AgentKey, directory_url: str | None = None, headless: bool = True):
        self.key = key
        self.directory_url = directory_url
        self.headless = headless
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self.viewport = {"width": 900, "height": 560}

    async def _ensure(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.headless)
        return self._browser

    async def pay(self, checkout_url: str, card: CardDetails, timeout_s: float = 40) -> dict:
        async with self._lock:
            browser = await self._ensure()
            ctx = await browser.new_context(viewport=self.viewport, device_scale_factor=2)
            merchant = urlsplit(checkout_url).netloc
            try:
                page = await ctx.new_page()

                async def sign(route, request):
                    if urlsplit(request.url).netloc == merchant:
                        h = tap.sign_request(self.key, request.method, request.url, None, tap.TAG_PAY,
                                             directory_url=self.directory_url)
                        await route.continue_(headers={**request.headers, **h})
                    else:
                        await route.continue_()

                await page.route("**/*", sign)
                await page.goto(checkout_url)
                await page.wait_for_function("document.title === 'ready' || document.title === 'failed'",
                                             timeout=timeout_s * 1000)
                if await page.title() == "failed":
                    return {"ok": False, "detail": await page.inner_text("#out")}
                frame = None
                for _ in range(60):
                    frame = next((f for f in page.frames if "elements/card" in f.url), None)
                    if frame and await frame.locator("input[name=cardnumber-input]").count():
                        break
                    await asyncio.sleep(0.25)
                await frame.locator("input[name=cardnumber-input]").type(card.number, delay=20)
                await frame.locator("input[name=expiry-input]").type(card.expiry_month + card.expiry_year[-2:], delay=20)
                await frame.locator("input[name=cvc-input]").type(card.cvc, delay=20)
                shots = os.environ.get("KEYSTONE_CHECKOUT_SHOTS")
                if shots:  # for the demo film: the checkout as the agent's browser sees it
                    os.makedirs(shots, exist_ok=True)
                    await page.wait_for_timeout(400)
                    await page.screenshot(path=os.path.join(shots, f"checkout_{int(time.time() * 1000)}.png"))
                await page.click("#pay")
                await page.wait_for_function("document.title === 'held' || document.title === 'failed'",
                                             timeout=timeout_s * 1000)
                return {"ok": await page.title() == "held", "detail": await page.inner_text("#out")}
            finally:
                await ctx.close()

    async def close(self):
        if self._browser:
            await self._browser.close()
            await self._pw.stop()
            self._browser = None
