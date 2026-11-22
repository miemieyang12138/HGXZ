from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import json
import logging
import os
import random
import time

from .discovery import DEFAULT_ANNOUNCE_URLS, fetch_mirror_candidates


LOG = logging.getLogger(__name__)


class HuangguoClient:
    def __init__(
        self,
        base_url: str,
        request_interval: float = 2.0,
        timeout: int = 30,
        backup_urls: list[str] | None = None,
        *,
        announce_urls: list[str] | None = None,
        config_path: str | Path | None = None,
    ):
        urls = [base_url, *(backup_urls or [])]
        self.base_urls = list(dict.fromkeys(url.rstrip('/') for url in urls if url))
        if not self.base_urls:
            raise ValueError('at least one site base URL is required')
        self.base_url = self.base_urls[0]
        self.request_interval = request_interval
        self.timeout = timeout
        self.last_request = 0.0
        self.user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36'
        self._backup_requests = 0
        self.announce_urls = list(announce_urls) if announce_urls is not None else list(DEFAULT_ANNOUNCE_URLS)
        self.config_path = Path(config_path) if config_path else None

    def _throttle(self) -> None:
        wait = self.request_interval - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, min(0.25, self.request_interval / 4)))

    def _candidates(self, is_absolute: bool) -> list[str | None]:
        if is_absolute:
            return [None]
        candidates = [self.base_url, *[url for url in self.base_urls if url != self.base_url]]
        if self.base_url != self.base_urls[0]:
            # After a failover, probe the primary host now and then so a
            # transient outage does not pin us to a backup forever.
            self._backup_requests += 1
            if self._backup_requests >= 20:
                self._backup_requests = 0
                candidates = [self.base_urls[0], self.base_url, *[url for url in self.base_urls if url not in (self.base_urls[0], self.base_url)]]
        return candidates

    def _persist_urls(self) -> None:
        if not self.config_path:
            return
        try:
            config = json.loads(self.config_path.read_text(encoding='utf-8'))
            site = config.setdefault('site', {})
            site['base_url'] = self.base_url
            site['backup_urls'] = [url for url in self.base_urls if url != self.base_url]
            # Write atomically so a crash mid-write cannot corrupt the config.
            tmp_path = self.config_path.with_name(self.config_path.name + '.tmp')
            tmp_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.replace(tmp_path, self.config_path)
            LOG.info('persisted site URLs to %s', self.config_path)
        except Exception as exc:
            LOG.warning('failed to persist site URLs to %s: %s', self.config_path, exc)

    def _probe(self, base: str) -> bool:
        """Check a mirror is the real site via the anonymous search API."""
        req = Request(
            base + '/api/search?q=a',
            headers={'User-Agent': self.user_agent, 'Referer': base + '/'})
        try:
            with urlopen(req, timeout=min(self.timeout, 15)) as response:
                payload = json.loads(response.read())
            return payload.get('status') == 1
        except Exception:
            return False

    def _refresh_addresses(self) -> bool:
        LOG.warning('all configured site URLs failed; fetching fresh mirrors from announce pages')
        candidates = fetch_mirror_candidates(
            self.announce_urls, timeout=self.timeout, user_agent=self.user_agent)
        if not candidates:
            LOG.error('no mirror candidates found on announce pages')
            return False
        working: list[str] = []
        for candidate in candidates[:24]:  # cap probing cost
            if self._probe(candidate):
                working.append(candidate)
                LOG.info('mirror alive: %s', candidate)
                if len(working) >= 6:
                    break
            else:
                LOG.debug('mirror dead: %s', candidate)
        if not working:
            LOG.error('none of the discovered mirrors responded')
            return False
        self.base_urls = working
        self.base_url = working[0]
        self._backup_requests = 0
        self._persist_urls()
        LOG.warning('site URLs refreshed; base_url=%s (%d mirrors)', self.base_url, len(working))
        return True

    def _try_request(self, method: str, path: str, data: bytes | None, headers: dict | None,
                     expect_json: bool) -> bytes:
        is_absolute = path.startswith('http')
        candidates = self._candidates(is_absolute)
        last_error: Exception | None = None
        for index, candidate in enumerate(candidates):
            url = path if is_absolute else candidate + path
            referer = self.base_url + '/' if is_absolute else candidate + '/'
            request_headers = {'User-Agent': self.user_agent, 'Referer': referer}
            request_headers.update(headers or {})
            req = Request(url, method=method, data=data, headers=request_headers)
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    body = response.read()
                if expect_json:
                    try:
                        json.loads(body)
                    except ValueError as error:
                        # A hijacked domain answers 200 with garbage HTML
                        # instead of failing; treat that as a dead mirror.
                        raise RuntimeError(
                            f'non-JSON response from {url}: possible domain hijack') from error
                if candidate and candidate != self.base_url:
                    LOG.warning('site base URL switched to: %s', candidate)
                    self.base_url = candidate
                    self._backup_requests = 0
                    self._persist_urls()
                return body
            except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, RuntimeError) as error:
                last_error = error
                if is_absolute or index == len(candidates) - 1:
                    raise
                LOG.warning('site request failed via %s; trying backup: %s', candidate, error)
        raise last_error or RuntimeError('site request failed')

    def _request(self, method: str, path: str, *, data: bytes | None = None, headers: dict | None = None,
                 expect_json: bool = False) -> bytes:
        self._throttle()
        is_absolute = path.startswith('http')
        try:
            return self._try_request(method, path, data, headers, expect_json)
        except (HTTPError, URLError, TimeoutError, ConnectionError, OSError, RuntimeError):
            if is_absolute or not self.announce_urls:
                raise
            if not self._refresh_addresses():
                raise
            self._throttle()
            return self._try_request(method, path, data, headers, expect_json)
        finally:
            self.last_request = time.monotonic()

    def get_text(self, path: str) -> str:
        return self._request('GET', path).decode('utf-8', errors='replace')

    def get_bytes(self, url: str) -> bytes:
        return self._request('GET', url)

    def play_url(self, album_id: int, episode: int) -> str:
        # Since the 2026-11 site redesign the signed HLS manifest is served by
        # POST /api/videos/<id>/episodes/<ep>/playback; the old GET /play
        # endpoint only returns empty video_url fields now.
        raw = self._request(
            'POST', f'/api/videos/{int(album_id)}/episodes/{int(episode)}/playback',
            data=b'{}', headers={'Content-Type': 'application/json'}, expect_json=True)
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'playback endpoint failed')
        data = payload.get('data') or {}
        if data.get('manifest_method') != 'GET':
            raise RuntimeError('playback returned no GET manifest')
        url = ((data.get('streams') or {}).get('main') or '').strip()
        if not url:
            raise RuntimeError('empty playback stream')
        return url

    def search(self, keyword: str, page: int = 1) -> list[dict]:
        path = '/api/search?q=' + quote(str(keyword))
        if int(page) > 1:
            path += f'&page={int(page)}'
        raw = self._request('GET', path, expect_json=True)
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'search failed')
        data = payload.get('data') or {}
        return list(data.get('items') or [])

    def cover_url(self, album_id: int) -> str:
        body = json.dumps({'items': [{'type': 'video', 'id': int(album_id)}]}, ensure_ascii=False).encode()
        raw = self._request('POST', '/api/media/covers', data=body,
                            headers={'Content-Type': 'application/json'}, expect_json=True)
        payload = json.loads(raw)
        if payload.get('status') != 1:
            raise RuntimeError(payload.get('msg') or 'cover endpoint failed')
        return (((payload.get('data') or {}).get('covers') or {}).get(f'video:{int(album_id)}') or '').strip()
