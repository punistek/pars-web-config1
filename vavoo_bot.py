#!/usr/bin/env python3
"""Vavoo playlist publisher: preserve all source channels, report URL changes.

Requires: Python 3.11+, requests. Does not attempt authentication bypass or proxying.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unicodedata
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('vavoo')
_thread_local = threading.local()
M3U_ATTR = re.compile(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s]+))')
MEDIA_EXT = ('.m3u8', '.m3u', '.mpd', '.ts', '.m4s', '.mp4', '.aac', '.mp3', '.flv')
HTTP_FIELDS = {'user-agent': 'User-Agent', 'referer': 'Referer', 'referrer': 'Referer',
               'origin': 'Origin', 'cookie': 'Cookie'}
COUNTRY_ALIASES = {
    'turkey': 'TR', 'turkiye': 'TR', 'turkei': 'TR', 'turk': 'TR', 'tr': 'TR',
    'germany': 'DE', 'deutschland': 'DE', 'almanya': 'DE', 'de': 'DE',
    'united kingdom': 'GB', 'great britain': 'GB', 'uk': 'GB', 'gb': 'GB', 'england': 'GB',
    'usa': 'US', 'united states': 'US', 'us': 'US',
    'france': 'FR', 'fransa': 'FR', 'fr': 'FR',
    'spain': 'ES', 'espana': 'ES', 'ispanya': 'ES', 'es': 'ES',
    'italy': 'IT', 'italia': 'IT', 'italya': 'IT', 'it': 'IT',
    'netherlands': 'NL', 'holland': 'NL', 'nl': 'NL',
    'portugal': 'PT', 'pt': 'PT', 'poland': 'PL', 'polska': 'PL', 'pl': 'PL',
    'russia': 'RU', 'russian': 'RU', 'ru': 'RU',
    'azerbaijan': 'AZ', 'azerbaycan': 'AZ', 'az': 'AZ',
    'arabic': None, 'sports': None, 'movies': None,
}


@dataclass
class Channel:
    name: str
    url: str
    group: str = 'Diğer'
    tvg_id: str = ''
    logo: str = ''
    country: str = ''
    headers: dict[str, str] = field(default_factory=dict)
    status: str = 'not_checked'
    url_status: str = ''
    previous_url: str = ''


def clean_text(text: Any) -> str:
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(text or '')).strip()


def key_name(value: Any) -> str:
    s = unicodedata.normalize('NFKD', str(value or '').casefold())
    s = ''.join('i' if c == 'ı' else c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s)


def valid_http_url(url: str, allow_private: bool = False) -> bool:
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return False
        host = p.hostname.lower().rstrip('.')
        if not allow_private and (host == 'localhost' or host.endswith(('.local', '.internal', '.localhost'))):
            return False
        try:
            address = ipaddress.ip_address(host)
            if not allow_private and not address.is_global:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def request_session() -> requests.Session:
    # Each thread gets its own requests session (no shared mutable session state).
    session = getattr(_thread_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.trust_env = True
        session.mount('http://', HTTPAdapter(pool_connections=8, pool_maxsize=8))
        session.mount('https://', HTTPAdapter(pool_connections=8, pool_maxsize=8))
        _thread_local.session = session
    return session


def get_bytes(url: str, headers: dict[str, str], timeout: int, max_bytes: int,
              *, allow_private: bool = False, range_header: bool = False) -> tuple[bytes, str, str]:
    # Limit redirects ourselves: a public stream must not redirect probes into
    # private networks. Treat large source lists as errors, never publish a
    # silently truncated playlist.
    request_headers = {'User-Agent': 'VavooM3UBuilder/1.0', 'Accept': '*/*', **headers}
    if range_header:
        request_headers['Range'] = f'bytes=0-{max_bytes - 1}'
    current_url = url
    for _ in range(6):
        if not valid_http_url(current_url, allow_private):
            raise ValueError('Geçersiz veya özel ağ adresi/yönlendirme')
        with request_session().get(current_url, headers=request_headers, timeout=(timeout, timeout),
                                   stream=True, allow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('Yönlendirme adresi eksik')
                current_url = urljoin(response.url, location)
                continue
            response.raise_for_status()
            content = bytearray()
            read_limit = max_bytes if range_header else max_bytes + 1
            for block in response.iter_content(chunk_size=min(16384, max_bytes + 1)):
                content.extend(block)
                if len(content) >= read_limit:
                    break
            if not range_header and len(content) > max_bytes:
                raise ValueError('Kaynak ya da oynatma listesi boyut sınırını aşıyor')
            return bytes(content[:max_bytes]), response.headers.get('Content-Type', '').lower(), response.url
    raise ValueError('Çok fazla HTTP yönlendirme')


def split_extinf(value: str) -> tuple[dict[str, str], str]:
    # Commas in quoted tvg attributes are not title separators.
    quoted = False
    quote_char = ''
    idx = -1
    for i, c in enumerate(value):
        if c in ('"', "'"):
            if not quoted:
                quoted = True
                quote_char = c
            elif c == quote_char:
                quoted = False
        if c == ',' and not quoted:
            idx = i
            break
    if idx < 0:
        head, name = value, ''
    else:
        head, name = value[:idx], value[idx+1:]
    attrs = {m.group(1).lower(): next((x for x in m.groups()[1:] if x is not None), '')
             for m in M3U_ATTR.finditer(head)}
    return attrs, clean_text(name)


def extract_url_headers(url: str) -> tuple[str, dict[str, str]]:
    # Standard IPTV URL suffix: https://...m3u8|User-Agent=...&Referer=...
    headers: dict[str, str] = {}
    if '|' in url:
        maybe_url, opts = url.rsplit('|', 1)
        if opts and '=' in opts and maybe_url.startswith(('http://', 'https://')):
            for item in opts.split('&'):
                name, sep, val = item.partition('=')
                mapped = HTTP_FIELDS.get(name.casefold().replace('_', '-'))
                if sep and mapped:
                    headers[mapped] = clean_text(unquote(val))
            url = maybe_url
    return url.strip(), headers


def make_channel(name: str, url: str, origin: str, group: str = '', tvg_id: str = '',
                 logo: str = '', country: str = '', headers: dict[str, str] | None = None) -> Channel | None:
    if not name or not url:
        return None
    raw_url, suffix_headers = extract_url_headers(url.strip())
    absolute = urljoin(origin, raw_url)
    if not valid_http_url(absolute, allow_private=True):
        return None
    clean_headers = {HTTP_FIELDS[k.casefold().replace('_','-')]: clean_text(v)
                     for k,v in (headers or {}).items()
                     if k.casefold().replace('_','-') in HTTP_FIELDS and v}
    clean_headers.update(suffix_headers)
    return Channel(name=clean_text(name), url=absolute, group=clean_text(group) or 'Diğer',
                   tvg_id=clean_text(tvg_id), logo=clean_text(logo),
                   country=clean_text(country).upper(), headers=clean_headers)


def parse_m3u(body: str, origin: str) -> list[Channel]:
    if '#EXTINF' not in body.upper():
        raise ValueError('Kaynak M3U kanal listesi içermiyor')
    parsed: list[Channel] = []
    attrs: dict[str, str] | None = None
    name = ''
    headers: dict[str, str] = {}
    extgrp = ''
    for raw in body.splitlines():
        line = raw.strip().lstrip('\ufeff')
        if not line:
            continue
        upper = line.upper()
        if upper.startswith('#EXTINF:'):
            attrs, name = split_extinf(line[8:])
            headers = {}
            extgrp = ''
        elif upper.startswith('#EXTGRP:') and attrs is not None:
            extgrp = line.partition(':')[2].strip()
        elif upper.startswith('#EXTVLCOPT:') and attrs is not None:
            item = line.partition(':')[2]
            k, sep, v = item.partition('=')
            k = k.lower().strip().removeprefix('http-')
            mapped = HTTP_FIELDS.get(k)
            if sep and mapped:
                headers[mapped] = v.strip()
        elif upper.startswith('#EXTHTTP:') and attrs is not None:
            try:
                obj = json.loads(line.partition(':')[2])
                if isinstance(obj, dict):
                    headers.update({HTTP_FIELDS[k.casefold()]: str(v)
                                    for k, v in obj.items() if k.casefold() in HTTP_FIELDS})
            except ValueError:
                pass
        elif not line.startswith('#') and attrs is not None:
            # Some providers put request headers directly in #EXTINF attributes.
            # Keep declared headers; do not invent a Vavoo token or bypass access control.
            for attr, raw_value in attrs.items():
                header_name = attr.removeprefix('http-')
                mapped = HTTP_FIELDS.get(header_name)
                if mapped and raw_value:
                    headers.setdefault(mapped, raw_value)
            channel = make_channel(name or attrs.get('tvg-name', ''), line, origin,
                                   attrs.get('group-title', '') or extgrp, attrs.get('tvg-id', ''),
                                   attrs.get('tvg-logo', ''), attrs.get('tvg-country', ''), headers)
            if channel:
                parsed.append(channel)
            attrs, name, headers, extgrp = None, '', {}, ''
    return parsed


def parse_json(body: str, origin: str) -> list[Channel]:
    data = json.loads(body)
    channels: list[Channel] = []
    def scan(value: Any, default_group: str = '', depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, list):
            for item in value:
                scan(item, default_group, depth+1)
        elif isinstance(value, dict):
            name = next((value.get(k) for k in ('name', 'title', 'channel_name', 'label', 'channel')
                         if isinstance(value.get(k), str)), '')
            link = next((value.get(k) for k in ('url', 'stream_url', 'stream', 'link', 'm3u8', 'src')
                         if isinstance(value.get(k), str) and value.get(k)), '')
            group = next((value.get(k) for k in ('group-title', 'group', 'country', 'category')
                          if isinstance(value.get(k), str) and value.get(k)), default_group)
            if name and link:
                ch = make_channel(name, link, origin, group,
                                  str(value.get('tvg-id') or value.get('tvg_id') or ''),
                                  str(value.get('tvg-logo') or value.get('logo') or ''),
                                  str(value.get('tvg-country') or ''),
                                  value.get('headers') if isinstance(value.get('headers'), dict) else {})
                if ch:
                    channels.append(ch)
                return
            for k, item in value.items():
                if isinstance(item, (list, dict)):
                    scan(item, (k if isinstance(item, list) and k not in ('data','channels','items','results','streams','list')
                                else default_group), depth+1)
    scan(data)
    if not channels:
        raise ValueError('JSON içinde kanal adı ve yayın adresi bulunamadı')
    return channels


def channel_name_aliases(name: str) -> list[str]:
    # Strip only broadcast quality labels, not meaningful names/numbers.
    variants = [key_name(name)]
    cleaned = re.sub(r'(?i)\s*(?:\((?:4k|uhd|fhd|hd|sd|1080p|720p)\)|\[(?:4k|uhd|fhd|hd|sd|1080p|720p)\]|(?:4k|uhd|fhd|hd|sd|1080p|720p))\s*$', '', name).strip()
    if key_name(cleaned) and key_name(cleaned) != variants[0]:
        variants.append(key_name(cleaned))
    return variants


def infer_country(ch: Channel) -> str:
    if re.fullmatch(r'[A-Za-z]{2}', ch.country):
        return ch.country.upper()
    for raw in (ch.group,):
        # Allow country + subcategory names such as 'Turkey Sport' / 'Türk Haber'.
        group_ascii = unicodedata.normalize('NFKD', raw.casefold()).encode('ascii', 'ignore').decode()
        if re.match(r'^\s*(?:turkey|turkiye|turk|tr)\b', group_ascii):
            return 'TR'
        words = re.split(r'\s*[|/,:\-]\s*', raw)
        for part in [raw, *words]:
            code = COUNTRY_ALIASES.get(unicodedata.normalize('NFKD', part.casefold()).encode('ascii', 'ignore').decode().strip())
            if code:
                return code
    m = re.match(r'^\s*(?:\[|\()?\s*(TR|DE|GB|US|FR|ES|IT|NL|PT|PL|RU|AZ)\s*(?:\]|\)|\||:)', ch.name, re.I)
    return m.group(1).upper() if m else ''


class Metadata:
    def __init__(self, settings: dict[str, Any], timeout: int) -> None:
        self.settings = settings
        self.timeout = timeout
        self.by_name: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self.logo_by_id: dict[str, str] = {}

    def load(self) -> None:
        if not self.settings.get('enabled', True):
            return
        try:
            b, _, _ = get_bytes(self.settings['channels_url'], {}, self.timeout, 24_000_000)
            data = json.loads(b.decode('utf-8-sig'))
            for item in data:
                if not isinstance(item, dict) or not item.get('id') or not item.get('name'):
                    continue
                ch_id, country = str(item['id']), str(item.get('country') or '').upper()
                for name in [item['name'], *(item.get('alt_names') or [])]:
                    if name:
                        entry = (ch_id, country, str(item['name']))
                        if entry not in self.by_name[key_name(name)]:
                            self.by_name[key_name(name)].append(entry)
        except Exception as exc:
            LOG.warning('EPG kataloğu alınamadı: %s', exc)
        if not self.by_name:
            return
        try:
            b, _, _ = get_bytes(self.settings['logos_url'], {}, self.timeout, 25_000_000)
            logos = json.loads(b.decode('utf-8-sig'))
            choices: dict[str, tuple[int, str]] = {}
            for item in logos:
                if not isinstance(item, dict) or not item.get('channel') or not item.get('url'):
                    continue
                url = str(item['url'])
                if not valid_http_url(url):
                    continue
                fmt = str(item.get('format') or '').upper()
                # Prefer the most compatible logo formats and currently used logos.
                rank = (100 if item.get('in_use') else 0) + {'PNG': 30, 'WEBP': 20, 'JPEG': 10}.get(fmt, 0)
                if item.get('feed'):
                    rank -= 5
                code = str(item['channel'])
                if code not in choices or rank > choices[code][0]:
                    choices[code] = (rank, url)
            self.logo_by_id = {k: v[1] for k, v in choices.items()}
        except Exception as exc:
            LOG.warning('Logo kataloğu alınamadı: %s', exc)
        LOG.info('EPG katalog adı: %s, logo: %s', len(self.by_name), len(self.logo_by_id))

    def enrich(self, channel: Channel, overrides: dict[str, Any]) -> None:
        override = overrides.get(channel.name) or overrides.get(key_name(channel.name))
        if isinstance(override, dict):
            channel.tvg_id = clean_text(override.get('tvg_id') or channel.tvg_id)
            channel.logo = clean_text(override.get('logo') or channel.logo)
            channel.group = clean_text(override.get('group') or channel.group)
        if not channel.tvg_id or not channel.logo:
            country = infer_country(channel)
            for alias in channel_name_aliases(channel.name):
                options = self.by_name.get(alias, [])
                if country:
                    options = [o for o in options if o[1] == country]
                # Never assign ambiguous channel IDs based on a similar name alone.
                unique_ids = set(i[0] for i in options)
                if len(unique_ids) == 1:
                    match_id = next(iter(unique_ids))
                    if not channel.tvg_id:
                        channel.tvg_id = match_id
                    if not channel.logo:
                        channel.logo = self.logo_by_id.get(match_id, '')
                    break
        if not channel.logo and channel.tvg_id:
            channel.logo = self.logo_by_id.get(channel.tvg_id, '')


def check_hls(url: str, headers: dict[str, str], timeout: int, check_segments: bool,
              allow_private: bool, depth: int = 0) -> str:
    if depth > 3:
        raise ValueError('Çok katmanlı HLS oynatma listesi')
    body, content_type, final_url = get_bytes(url, headers, timeout, 750_000, allow_private=allow_private)
    text = body.decode('utf-8-sig', errors='replace').strip()
    if not text.startswith('#EXTM3U') or '#EXT-X-' not in text:
        raise ValueError(f'Geçersiz HLS (type: {content_type})')
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith('#EXT-X-STREAM-INF') for l in lines):
        variants: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF'):
                matches = re.search(r'BANDWIDTH=(\d+)', line)
                band = int(matches.group(1)) if matches else 0
                for candidate in lines[i+1:]:
                    if not candidate.startswith('#'):
                        variants.append((band, urljoin(final_url, candidate)))
                        break
        if not variants:
            raise ValueError('HLS master içinde varyant bulunamadı')
        # Prefer lowest bandwidth for a low-cost liveness probe.
        return check_hls(sorted(variants)[0][1], headers, timeout, check_segments,
                         allow_private, depth+1)
    if '#EXT-X-MEDIA-SEQUENCE' not in text and '#EXTINF:' not in text and '#EXT-X-MAP' not in text:
        raise ValueError('HLS içinde medya segmenti yok')
    if not check_segments:
        return 'hls_playlist'
    candidates = []
    for i, line in enumerate(lines):
        if line.startswith('#EXTINF:'):
            for candidate in lines[i+1:]:
                if candidate.startswith('#'):
                    continue
                candidates.append(urljoin(final_url, candidate))
                break
    if not candidates:
        for line in lines:
            match = re.search(r'#EXT-X-MAP:.*?URI="([^"]+)"', line)
            if match:
                candidates.append(urljoin(final_url, match.group(1)))
    if not candidates:
        raise ValueError('Segment adresi bulunamadı')
    # Avoid retrying multiple segments; transient errors can be retried next scheduled run.
    sample, ct, _ = get_bytes(candidates[0], headers, timeout, 1024,
                              allow_private=allow_private, range_header=True)
    if not sample or sample.lstrip().lower().startswith((b'<html', b'<!doctype html', b'{"error"')):
        raise ValueError('Segment boş veya hata sayfası')
    if 'text/html' in ct or 'application/json' in ct:
        raise ValueError(f'Segment medya değil: {ct}')
    return 'hls_segment'


def probe(channel: Channel, timeout: int, check_segments: bool, allow_private: bool) -> tuple[bool, str]:
    try:
        if not valid_http_url(channel.url, allow_private):
            return False, 'Özel/geçersiz URL'
        path = urlparse(channel.url).path.lower()
        if path.endswith(('.m3u8', '.m3u')):
            return True, check_hls(channel.url, channel.headers, timeout, check_segments, allow_private)
        sample, ct, final = get_bytes(channel.url, channel.headers, timeout, 8192,
                                      allow_private=allow_private, range_header=True)
        if sample.startswith(b'#EXTM3U'):
            return True, check_hls(final, channel.headers, timeout, check_segments, allow_private)
        if not sample or 'text/html' in ct or 'application/json' in ct:
            return False, 'Boş yanıt veya HTML/JSON'
        head = sample[:64].lstrip().lower()
        if head.startswith((b'<html', b'<!doctype', b'<?xml', b'{"error"', b'forbidden')):
            return False, 'Medya yerine hata sayfası'
        if ('video/' in ct or 'audio/' in ct or 'application/octet-stream' in ct
                or 'application/vnd.apple.mpegurl' in ct
                or sample.startswith((b'FLV', b'ID3', b'\x47', b'\xff\xf1', b'\xff\xf9'))
                or b'ftyp' in sample[:16] or b'styp' in sample[:16]):
            return True, 'http_media'
        return False, f'Bilinmeyen medya yanıtı ({ct or "no content-type"})'
    except (requests.RequestException, ValueError, UnicodeError) as exc:
        return False, f'{type(exc).__name__}: {str(exc)[:140]}'


def xml_attr(value: str) -> str:
    return (clean_text(value).replace('&', '&amp;').replace('"', '&quot;')
            .replace('<', '&lt;').replace('>', '&gt;'))


def output_m3u(channels: list[Channel], epg_url: str = '') -> str:
    header = '#EXTM3U'
    if epg_url:
        header += f' url-tvg="{xml_attr(epg_url)}" x-tvg-url="{xml_attr(epg_url)}"'
    lines = [header]
    for ch in channels:
        attrs = (f'tvg-id="{xml_attr(ch.tvg_id)}" tvg-name="{xml_attr(ch.name)}" '
                 f'tvg-logo="{xml_attr(ch.logo)}" group-title="{xml_attr(ch.group)}"')
        lines.append(f'#EXTINF:-1 {attrs},{clean_text(ch.name)}')
        for k, tag in [('User-Agent','http-user-agent'), ('Referer','http-referrer'),
                       ('Origin','http-origin'), ('Cookie','http-cookie')]:
            if k in ch.headers:
                lines.append(f'#EXTVLCOPT:{tag}={ch.headers[k]}')
        lines.append(ch.url)
    return '\n'.join(lines) + '\n'


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    try:
        tmp.write_text(content, encoding='utf-8')
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8-sig'))


def load_previous_entries(out_dir: Path) -> list[dict[str, Any]]:
    """Load previous URLs, expanding legacy 'sources' without counting them twice."""
    previous_path = out_dir / 'vavoo.json'
    if not previous_path.exists():
        return []
    try:
        previous = read_json(previous_path)
    except (OSError, ValueError) as exc:
        LOG.warning('Önceki JSON okunamadı; URL değişimi geçmişi sıfır: %s', exc)
        return []
    if not isinstance(previous, list):
        LOG.warning('Önceki JSON kanal listesi değil; URL değişimi geçmişi sıfır')
        return []
    flattened: list[dict[str, Any]] = []
    for entry in previous:
        if not isinstance(entry, dict):
            continue
        variants = entry.get('sources')
        if not isinstance(variants, list) or not variants:
            variants = [entry]
        for item in variants:
            if isinstance(item, dict) and isinstance(item.get('url'), str):
                flattened.append({'name': entry.get('name', ''),
                                  'group': entry.get('group', 'Diğer'),
                                  'url': item['url']})
    return flattened


def compare_channel_urls(channels: list[Channel], previous: list[dict[str, Any]]) -> dict[str, Any]:
    """Multiset comparison of URLs within name+group.

    Match exact URLs first, then pair unmatched URLs as changes. This handles
    multiple sources for one channel without discarding any alternative.
    """
    current_groups: dict[tuple[str, str], list[Channel]] = defaultdict(list)
    previous_groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for ch in channels:
        current_groups[(key_name(ch.group), key_name(ch.name))].append(ch)
    for item in previous:
        if not item.get('url'):
            continue
        group = key_name(item.get('group', 'Diğer'))
        name = key_name(item.get('name', ''))
        previous_groups[(group, name)].append({'url': str(item['url']),
                                               'name': str(item.get('name', '')),
                                               'group': str(item.get('group', 'Diğer'))})

    changes: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for key in sorted(set(current_groups) | set(previous_groups)):
        now = current_groups.get(key, [])
        before = previous_groups.get(key, [])
        by_url: dict[str, deque[int]] = defaultdict(deque)
        for i, item in enumerate(before):
            by_url[item['url']].append(i)
        consumed: set[int] = set()
        unmatched: list[Channel] = []
        for channel in now:
            old_matches = by_url.get(channel.url)
            if old_matches:
                consumed.add(old_matches.popleft())
                channel.url_status = 'unchanged'
                counts['unchanged'] += 1
            else:
                unmatched.append(channel)
        remaining = [item for i, item in enumerate(before) if i not in consumed]
        for channel, old in zip(unmatched, remaining):
            old_url = old['url']
            channel.url_status = 'changed'
            channel.previous_url = old_url
            counts['changed'] += 1
            changes.append({'name': channel.name, 'group': channel.group,
                            'old_url': old_url, 'new_url': channel.url})
        for channel in unmatched[len(remaining):]:
            channel.url_status = 'new'
            counts['new'] += 1
        for old in remaining[len(unmatched):]:
            counts['removed'] += 1
            removed.append({'name': old['name'], 'group': old['group'],
                            'old_url': old['url']})
    return {'unchanged': counts['unchanged'], 'changed': counts['changed'],
            'new': counts['new'], 'removed': counts['removed'],
            'changes': changes, 'removed_entries': removed}


def channel_json(channel: Channel) -> dict[str, Any]:
    return {'name': channel.name, 'url': channel.url, 'group': channel.group,
            'tvg_id': channel.tvg_id, 'logo': channel.logo,
            'country': infer_country(channel), 'headers': channel.headers,
            'probe': channel.status, 'url_status': channel.url_status,
            'previous_url': channel.previous_url,
            # This entry is an independent source; no alternatives are collapsed.
            'sources': [{'url': channel.url, 'headers': channel.headers,
                         'probe': channel.status}]}


def build(settings: dict[str, Any], path: Path) -> dict[str, Any]:
    source = str(settings['source_url'])
    timeout = min(30, max(2, int(settings.get('timeout_seconds', 8))))
    workers = min(48, max(1, int(settings.get('workers', 20))))
    allow_private = bool(settings.get('allow_private_urls', False))
    out_dir = (path.parent / settings.get('output_dir', 'output')).resolve()
    started = datetime.now(timezone.utc).isoformat(timespec='seconds')
    LOG.info('Kaynak alınıyor: %s', source)
    b, ct, origin = get_bytes(source, {}, timeout, 24_000_000, allow_private=allow_private)
    content = b.decode('utf-8-sig', errors='replace')
    if content.lstrip().startswith(('{', '[')) or 'json' in ct:
        channels = parse_json(content, origin)
    else:
        channels = parse_m3u(content, origin)
    LOG.info('Kaynaktaki toplam kanal: %d', len(channels))
    if not channels:
        raise RuntimeError('Kaynakta kanal bulunamadı; eski liste korunuyor')

    # Never let a temporary empty/partial upstream response replace a full list.
    minimum_source = max(1, int(settings.get('minimum_source_channels', 1)))
    if len(channels) < minimum_source:
        raise RuntimeError(f'Kaynak kanal sayısı {len(channels)} < {minimum_source}; eski liste korunuyor')
    previous_status_path = out_dir / 'status.json'
    previous_status = {}
    if previous_status_path.exists():
        try:
            previous_status = read_json(previous_status_path)
        except (OSError, ValueError):
            pass
    previous_total = previous_status.get('input_channels', 0) if isinstance(previous_status, dict) else 0
    min_ratio = float(settings.get('minimum_source_ratio', 0))
    if previous_total and min_ratio > 0 and len(channels) < int(previous_total * min_ratio):
        raise RuntimeError(f'Kaynak aniden küçüldü ({previous_total} -> {len(channels)}); eski liste korunuyor')

    meta = Metadata(settings.get('metadata', {}), timeout)
    meta.load()
    override_path = (path.parent / settings.get('epg_overrides', 'epg_overrides.json')).resolve()
    overrides = read_json(override_path) if override_path.exists() else {}
    for ch in channels:
        meta.enrich(ch, overrides)

    # URL-change checking compares SOURCE URLs over consecutive runs. This is
    # deliberately not a stream health check, so offline/slow sources stay listed.
    comparisons = compare_channel_urls(channels, load_previous_entries(out_dir))
    probe_mode = str(settings.get('probe_mode', 'url_change_only')).strip().lower()
    if probe_mode not in ('url_change_only', 'health_report_only'):
        raise ValueError('probe_mode: url_change_only veya health_report_only olmalı')
    errors: Counter[str] = Counter()
    passed = failed = 0
    unique_urls: dict[tuple[str, tuple[tuple[str, str], ...]], Channel] = {}
    if probe_mode == 'health_report_only':
        for ch in channels:
            unique_urls.setdefault((ch.url, tuple(sorted(ch.headers.items()))), ch)
        LOG.info('Bilgilendirme amaçlı yayın testi: %d URL (listeden eleme yok)', len(unique_urls))
        checked: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[bool, str]] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(probe, ch, timeout, bool(settings.get('check_hls_segments', False)), allow_private): key
                       for key, ch in unique_urls.items()}
            for future in as_completed(futures):
                checked[futures[future]] = future.result()
        for ch in channels:
            ok, reason = checked[(ch.url, tuple(sorted(ch.headers.items())))]
            ch.status = reason
            if ok:
                passed += 1
            else:
                failed += 1
                errors[reason] += 1

    # Full means full: no filtering on health and no name/URL deduplication.
    # Keep source ordering; it is stable and preserves alternative streams.
    turkey = [ch for ch in channels if infer_country(ch) == 'TR']
    min_turkey = max(0, int(settings.get('minimum_turkey_channels', 0)))
    if len(turkey) < min_turkey:
        raise RuntimeError(f'Türkiye kanal sayısı {len(turkey)} < {min_turkey}; eski liste korunuyor')
    prior_turkey = previous_status.get('turkey_channels', 0) if isinstance(previous_status, dict) else 0
    min_turkey_ratio = float(settings.get('minimum_turkey_ratio', 0))
    if prior_turkey and min_turkey_ratio > 0 and len(turkey) < int(prior_turkey * min_turkey_ratio):
        raise RuntimeError(f'Türkiye kaynağı aniden küçüldü ({prior_turkey} -> {len(turkey)}); eski liste korunuyor')
    epg_url = str(settings.get('xmltv_url') or '')
    all_json = [channel_json(ch) for ch in channels]
    turkey_json = [channel_json(ch) for ch in turkey]
    report = {
        'updated_at_utc': started, 'source': source,
        'input_channels': len(channels), 'published_channels': len(channels),
        'turkey_channels': len(turkey), 'probe_mode': probe_mode,
        'health_checked_urls': len(unique_urls), 'health_passed': passed,
        'health_failed': failed, 'not_removed_due_to_failed_probe': failed,
        'url_unchanged': comparisons['unchanged'],
        'url_changed': comparisons['changed'], 'url_new': comparisons['new'],
        'url_removed_from_source': comparisons['removed'],
        'with_epg_id': sum(bool(c.tvg_id) for c in channels),
        'with_logo': sum(bool(c.logo) for c in channels),
        'groups': dict(sorted(Counter(c.group for c in channels).items())),
        'sample_errors': dict(errors.most_common(15))
    }
    url_report = {'updated_at_utc': started, 'source': source,
                  'unchanged': comparisons['unchanged'],
                  'changed': comparisons['changed'], 'new': comparisons['new'],
                  'removed': comparisons['removed'],
                  'changes': comparisons['changes'],
                  'removed_entries': comparisons['removed_entries']}
    # Complete all validation and output serialization before writing to disk.
    contents = {
        'vavoo.m3u': output_m3u(channels, epg_url),
        'vavoo.json': json.dumps(all_json, ensure_ascii=False, indent=2) + '\n',
        'turkiye.m3u': output_m3u(turkey, epg_url),
        'turkiye.json': json.dumps(turkey_json, ensure_ascii=False, indent=2) + '\n',
        'url_changes.json': json.dumps(url_report, ensure_ascii=False, indent=2) + '\n',
        'status.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
    }
    for filename, payload in contents.items():
        atomic_write(out_dir / filename, payload)
    LOG.info('Tam liste: %d | Türkiye: %d | Değişen URL: %d | Kaynaktan silinen: %d',
             len(channels), len(turkey), comparisons['changed'], comparisons['removed'])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description='Bağımsız Vavoo playlist botu')
    parser.add_argument('--config', default=str(ROOT / 'config.json'))
    parser.add_argument('--verbose', action='store_true')
    opts = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if opts.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    path = Path(opts.config).resolve()
    try:
        report = build(read_json(path), path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, requests.RequestException, ValueError, RuntimeError, KeyError, TypeError) as exc:
        LOG.error('Güncelleme başarısız, önceki çıktı korunuyor: %s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
