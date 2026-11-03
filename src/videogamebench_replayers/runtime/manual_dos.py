"""Optional human-recorder browser setup; benchmark action methods are unchanged."""

import hashlib
import json
import re
import socketserver


def configure_browser(headless, assets_dir, snapshots=None):
    from src.emulators.dos.browser_controller import BrowserController

    # Upstream uses TCPServer's default exclusive bind. Human restart creates a
    # new interpreter immediately, while the previous listener is in TIME_WAIT.
    socketserver.TCPServer.allow_reuse_address = True
    assets = {}
    if assets_dir is not None:
        for path in assets_dir.glob("*.json"):
            metadata = json.loads(path.read_text())
            data = path.with_suffix(".bin").read_bytes()
            if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                raise ValueError(f"Browser asset checksum mismatch: {path.name}")
            assets[metadata["url"]] = (metadata, data)
    if snapshots is not None:
        snapshots.patch_assets(assets)
    original = BrowserController.start

    async def start(browser):
        if headless:
            browser.headless = True
        await original(browser)
        if snapshots is not None:
            await snapshots.attach(browser)
        if assets:

            async def cached(route):
                entry = assets.get(route.request.url)
                if entry is None:
                    await route.continue_()
                    return
                metadata, data = entry
                await route.fulfill(
                    status=metadata["status"], headers=metadata["headers"], body=data
                )

            # Do not intercept binary state transfers through Playwright's JSON pipe.
            pattern = re.compile("^(?:" + "|".join(re.escape(url) for url in assets) + ")$")
            await browser.context.route(pattern, cached)

    BrowserController.start = start
