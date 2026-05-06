from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional
from urllib.parse import parse_qs

import zendriver as zd
from zendriver import Browser, Tab

from .config import settings
from .models import CookieInput, CookieOutput, SolutionResult
from .sessions import _base_browser_args, _stop_browser, session_manager

logger = logging.getLogger(__name__)

try:
    from zendriver import cdp
    _HAS_CDP = True
except ImportError:
    _HAS_CDP = False
    logger.warning("zendriver cdp module not available; cookie/media features limited")

try:
    from zendriver.core.cloudflare import cf_is_interactive_challenge_present
    _HAS_CF_DETECT = True
except ImportError:
    _HAS_CF_DETECT = False


async def solve_request(
    *,
    url: str,
    method: str = "GET",
    post_data: Optional[str] = None,
    cookies: Optional[List[CookieInput]] = None,
    proxy: Optional[str] = None,
    timeout_ms: int = 60000,
    return_only_cookies: bool = False,
    return_screenshot: bool = False,
    disable_media: bool = False,
    session_id: Optional[str] = None,
) -> SolutionResult:
    timeout_s = min(timeout_ms, settings.MAX_TIMEOUT) / 1000.0

    if session_id:
        entry = await session_manager.get(session_id)
        browser = entry.browser
        is_ephemeral = False
    else:
        browser = await _start_ephemeral(proxy)
        is_ephemeral = True

    tab: Optional[Tab] = None
    try:
        tab = await _open_tab(
            browser=browser,
            url=url,
            method=method,
            post_data=post_data,
            cookies=cookies,
            disable_media=disable_media,
            timeout_s=timeout_s,
        )
        captured_token = await _solve_cf(tab=tab, timeout_s=timeout_s)
        return await _collect(
            tab=tab,
            return_only_cookies=return_only_cookies,
            return_screenshot=return_screenshot,
            captured_token=captured_token,
        )
    finally:
        if tab is not None:
            try:
                await tab.close()
            except Exception:
                pass
        if is_ephemeral:
            await _stop_ephemeral(browser)


# ── Browser lifecycle ──────────────────────────────────────────────────────────

async def _start_ephemeral(proxy: Optional[str]) -> Browser:
    await session_manager.semaphore.acquire()
    try:
        args = _base_browser_args()
        if proxy:
            args.append(f"--proxy-server={proxy}")
        return await zd.start(
            headless=settings.HEADLESS,
            browser_executable_path=settings.BROWSER_EXECUTABLE_PATH,
            browser_args=args,
            sandbox=False,
        )
    except Exception:
        session_manager.semaphore.release()
        raise


async def _stop_ephemeral(browser: Browser) -> None:
    try:
        await _stop_browser(browser)
    finally:
        session_manager.semaphore.release()


# ── Tab setup ──────────────────────────────────────────────────────────────────

async def _open_tab(
    *,
    browser: Browser,
    url: str,
    method: str,
    post_data: Optional[str],
    cookies: Optional[List[CookieInput]],
    disable_media: bool,
    timeout_s: float,
) -> Tab:
    if method == "POST" and post_data:
        tab = await browser.get("about:blank", new_tab=True)
        if cookies:
            await _set_cookies(tab, cookies)
        if disable_media:
            await _block_media(tab)
        await _submit_post(tab, url, post_data)
    else:
        tab = await browser.get(url, new_tab=True)
        if cookies:
            await _set_cookies(tab, cookies)
        if disable_media:
            await _block_media(tab)

    await _wait_for_load(tab, timeout_s)
    return tab


async def _set_cookies(tab: Tab, cookies: List[CookieInput]) -> None:
    if not _HAS_CDP:
        return
    params = []
    for c in cookies:
        param = cdp.network.CookieParam(
            name=c.name,
            value=c.value,
            domain=c.domain,
            path=c.path or "/",
            secure=c.secure,
            http_only=c.httpOnly,
            expires=cdp.network.TimeSinceEpoch(c.expires) if c.expires else None,
        )
        params.append(param)
    try:
        await tab.send(cdp.storage.set_cookies(params))
    except Exception as e:
        logger.warning("Failed to set cookies via CDP: %s", e)


async def _block_media(tab: Tab) -> None:
    if not _HAS_CDP:
        return
    patterns = [
        "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.ico",
        "*.mp4", "*.webm", "*.mp3", "*.ogg",
        "*.woff", "*.woff2", "*.ttf", "*.eot",
    ]
    try:
        await tab.send(cdp.network.enable())
        await tab.send(cdp.network.set_blocked_ur_ls(urls=patterns))
    except Exception as e:
        logger.warning("Failed to block media via CDP: %s", e)


async def _submit_post(tab: Tab, url: str, post_data: str) -> None:
    fields = parse_qs(post_data, keep_blank_values=True)
    inputs_js = ""
    for key, values in fields.items():
        for val in values:
            safe_key = key.replace("\\", "\\\\").replace("'", "\\'")
            safe_val = val.replace("\\", "\\\\").replace("'", "\\'")
            inputs_js += (
                f"var i=document.createElement('input');"
                f"i.type='hidden';i.name='{safe_key}';i.value='{safe_val}';"
                f"f.appendChild(i);"
            )

    safe_url = url.replace("\\", "\\\\").replace("'", "\\'")
    js = f"""
    (function(){{
        var f=document.createElement('form');
        f.method='POST';
        f.action='{safe_url}';
        {inputs_js}
        document.body.appendChild(f);
        f.submit();
    }})();
    """
    try:
        await tab.evaluate(js)
    except Exception as e:
        logger.warning("POST form submission JS error: %s", e)


async def _wait_for_load(tab: Tab, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    js = "document.readyState === 'complete'"
    while True:
        try:
            if await tab.evaluate(js):
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Page did not finish loading within {timeout_s:.1f}s")
        await asyncio.sleep(0.5)


# ── Cloudflare solving ─────────────────────────────────────────────────────────

async def _solve_cf(tab: Tab, timeout_s: float) -> Optional[str]:
    """Solve CF challenge. Returns the raw turnstile token read via element.attrs."""
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Cloudflare challenge not solved within {timeout_s:.0f}s")

        if  await _is_cf_page(tab):
            logger.info("Cloudflare browser integrity check found... attempting to solve in 90 seconds")
            try:
                await asyncio.wait_for(
                    tab.verify_cf(timeout=90),
                    timeout=95,
                )
            except Exception as e:
                if "Node with given id does not belong to the document" in str(e):
                    continue
                logger.error("Failed to solve integrity check: %s", e)
                return None

        logger.info("Cloudflare challenge detected, attempting to solve (%.0fs remaining)", remaining)
        attempt_timeout = min(remaining, 30.0)

        try:
            await asyncio.wait_for(
                tab.verify_cf(timeout=attempt_timeout),
                timeout=attempt_timeout + 5,
            )
            return await _read_token_from_element(tab)
        except (asyncio.TimeoutError, TimeoutError):
            logger.debug("verify_cf timed out, retrying")
            token = await _read_token_from_element(tab)
            if token:
                return token
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.warning("verify_cf error: %s", e)
            # Stale-node errors fire when CF refreshes the DOM after solving.
            # Read the token immediately before the page can clear it.
            token = await _read_token_from_element(tab)
            logger.info("Token read after verify_cf error: %r", token)
            if token:
                return token
            # Log what _is_cf_page sees so we can tell if CF is cycling vs. solved
            still_cf = await _is_cf_page(tab)
            logger.info("CF page still detected after error: %s", still_cf)
            await asyncio.sleep(1.0)

    raise TimeoutError(f"Cloudflare challenge not solved within {timeout_s:.0f}s")


async def _read_token_from_element(tab: Tab) -> Optional[str]:
    """
    Read the CF turnstile token via CSS selector + element.attrs.
    Uses tab.select() (CSS selector) — NOT tab.find() which searches text content.
    element.attrs is the same dict that verify_cf checks internally.
    """
    try:
        el = await asyncio.wait_for(
            tab.select('input[name="cf-turnstile-response"]'),
            timeout=5.0,
        )
        if el is not None:
            val = el.attrs.get("value")
            if val:
                return val
            # Flat attributes list fallback
            attrs = el.attributes or []
            for idx, attr in enumerate(attrs):
                if attr == "value" and idx % 2 == 0:
                    token = attrs[idx + 1]
                    if token:
                        return token
    except asyncio.TimeoutError:
        logger.debug("Turnstile input not found within timeout")
    except Exception as e:
        logger.debug("Token element read failed: %s", e)

    # JS fallback: getAttribute reads the HTML attribute (set by CF via setAttribute)
    try:
        val = await asyncio.wait_for(
            tab.evaluate(
                "var _i=document.querySelector('input[name=\"cf-turnstile-response\"]');"
                "_i ? (_i.getAttribute('value') || _i.value || '') : ''"
            ),
            timeout=3.0,
        )
        return val if val else None
    except Exception:
        return None


async def _is_cf_page(tab: Tab) -> bool:
    # Title heuristic (JS challenge, "Just a moment...")
    try:
        title = await asyncio.wait_for(tab.evaluate("document.title"), timeout=3.0)
        if title and any(
            s in title.lower()
            for s in ["just a moment", "checking your browser", "please wait", "attention required", "ddos-guard"]
        ):
            return True
    except Exception:
        pass

    # DOM: CF challenge present AND turnstile NOT yet solved.
    # Once the turnstile token has a value the challenge is done — return False so we stop.
    try:
        js = """
        (function(){
            var resp = document.querySelector('input[name="cf-turnstile-response"]');
            if (resp && resp.value) return false;
            return !!(
                document.querySelector('#challenge-running') ||
                document.querySelector('#challenge-stage') ||
                document.querySelector('.cf-browser-verification') ||
                resp ||
                document.querySelector('iframe[src*="challenges.cloudflare.com"]')
            );
        })()
        """
        result = await asyncio.wait_for(tab.evaluate(js), timeout=3.0)
        if result:
            return True
    except Exception:
        pass

    if _HAS_CF_DETECT:
        try:
            return await cf_is_interactive_challenge_present(tab, timeout=3.0)
        except Exception:
            pass

    return False


# ── Result collection ──────────────────────────────────────────────────────────

async def _collect(
    *,
    tab: Tab,
    return_only_cookies: bool,
    return_screenshot: bool,
    captured_token: Optional[str] = None,
) -> SolutionResult:
    final_url = tab.url or ""

    user_agent = ""
    try:
        user_agent = await tab.evaluate("navigator.userAgent") or ""
    except Exception:
        pass

    output_cookies = await _get_cookies(tab)
    # Prefer the token captured right after verify_cf; fall back to reading the DOM
    # (useful if the page hasn't auto-submitted yet).
    turnstile_token = captured_token or await _get_turnstile_token(tab)

    html: Optional[str] = None
    if not return_only_cookies:
        html = await _get_html(tab)

    screenshot: Optional[str] = None
    if return_screenshot:
        screenshot = await _take_screenshot(tab)

    response_body = screenshot if (return_screenshot and screenshot) else html

    return SolutionResult(
        url=final_url,
        status=200,
        headers={},
        response=response_body,
        cookies=output_cookies,
        userAgent=user_agent,
        turnstile_token=turnstile_token or None,
    )


async def _get_turnstile_token(tab: Tab) -> Optional[str]:
    try:
        js = """
        window.__cf_token_captured ||
        document.querySelector('input[name="cf-turnstile-response"]')?.value ||
        ''
        """
        val = await asyncio.wait_for(tab.evaluate(js), timeout=3.0)
        return val if val else None
    except Exception:
        return None


async def _get_cookies(tab: Tab) -> List[CookieOutput]:
    if not _HAS_CDP:
        return []
    try:
        raw = await tab.send(cdp.storage.get_cookies())
        return [_convert_cookie(c) for c in raw]
    except Exception as e:
        logger.warning("Failed to get cookies via CDP: %s", e)
        return []


def _convert_cookie(c) -> CookieOutput:
    same_site = None
    if hasattr(c, "same_site") and c.same_site is not None:
        same_site = c.same_site.value if hasattr(c.same_site, "value") else str(c.same_site)
    return CookieOutput(
        name=c.name,
        value=c.value,
        domain=c.domain,
        path=c.path,
        expires=float(c.expires) if c.expires else None,
        size=getattr(c, "size", None),
        httpOnly=getattr(c, "http_only", False),
        secure=getattr(c, "secure", False),
        session=getattr(c, "session", False),
        sameSite=same_site,
    )


async def _get_html(tab: Tab) -> Optional[str]:
    try:
        return await tab.get_content()
    except Exception:
        pass
    try:
        return await tab.evaluate("document.documentElement.outerHTML")
    except Exception as e:
        logger.warning("Failed to get page HTML: %s", e)
        return None


async def _take_screenshot(tab: Tab) -> Optional[str]:
    if _HAS_CDP:
        try:
            data = await tab.send(cdp.page.capture_screenshot(format_="png"))
            return data if isinstance(data, str) else None
        except Exception:
            pass
    # Fallback to zendriver helper if available
    try:
        return await tab.screenshot_b64(format="png")
    except Exception as e:
        logger.warning("Screenshot failed: %s", e)
        return None
