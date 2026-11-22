from __future__ import annotations

from urllib.parse import urljoin
from urllib.request import Request, urlopen
import logging
import random
import re
import ssl


LOG = logging.getLogger(__name__)

# The announce pages are a plain address list and their certs have expired
# before (e.g. huang-guo.pages.dev in 2026-11); the list is only advisory
# anyway since every candidate gets probed over verified TLS before use.
# So fetch them without certificate verification.
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE

# The address-publish pages list the current mirror domains; each page loads a
# publish.js that holds the actual address data.
DEFAULT_ANNOUNCE_URLS = [
    'https://huang-guo.pages.dev/',
    'https://huangguoai.pages.dev/',
]


def _extract_array(js: str, name: str) -> list[str]:
    match = re.search(r'var\s+' + re.escape(name) + r'\s*=\s*\[(.*?)\]', js, re.S)
    if not match:
        return []
    return [item.strip() for item in re.findall(r'[\'"]([^\'"]+)[\'"]', match.group(1))]


def parse_publish_js(js: str) -> list[str]:
    """Build mirror candidates from a publish.js script.

    The script declares ``subdomains`` (random prefixes) and ``urls`` (bare
    domains). Live mirrors are random ``https://<sub>.<domain>`` pairings and
    the pairing is regenerated on every page load, so every combination is
    returned and the caller probes them to see which are actually alive.
    """
    subdomains = _extract_array(js, 'subdomains')
    domains = []
    for raw in _extract_array(js, 'urls'):
        domain = re.sub(r'^https?://', '', raw.strip().rstrip('/'))
        if domain:
            domains.append(domain)
    candidates: list[str] = []
    for domain in domains:
        if subdomains:
            candidates.extend(f'https://{sub}.{domain}' for sub in subdomains)
        else:
            candidates.append(f'https://{domain}')
    return candidates


def fetch_mirror_candidates(
    announce_urls: list[str] | None = None,
    *,
    timeout: int = 30,
    user_agent: str = 'Mozilla/5.0',
) -> list[str]:
    """Fetch the announce pages and return shuffled mirror candidates.

    All announce pages are tried so one dead page does not block the refresh;
    candidates are shuffled because the pages generate random pairings anyway.
    """
    urls = [u for u in (announce_urls or DEFAULT_ANNOUNCE_URLS) if u]
    seen: set[str] = set()
    out: list[str] = []
    for base in urls:
        try:
            req = Request(base, headers={'User-Agent': user_agent})
            with urlopen(req, timeout=timeout, context=_UNVERIFIED) as response:
                html = response.read().decode('utf-8', errors='replace')
            src = re.search(r'src=["\']([^"\']*publish\.js[^"\']*)["\']', html)
            js_url = urljoin(base, src.group(1)) if src else urljoin(base, 'publish.js')
            req = Request(js_url, headers={'User-Agent': user_agent})
            with urlopen(req, timeout=timeout, context=_UNVERIFIED) as response:
                js = response.read().decode('utf-8', errors='replace')
            found = parse_publish_js(js)
            LOG.info('announce page %s yielded %d mirror candidates', base, len(found))
            for candidate in found:
                if candidate not in seen:
                    seen.add(candidate)
                    out.append(candidate)
        except Exception as exc:
            LOG.warning('announce page %s failed: %s', base, exc)
    random.shuffle(out)
    return out
