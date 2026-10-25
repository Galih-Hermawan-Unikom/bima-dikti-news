"""Read-only announcement prototype. Imports no bot, credentials or state code.

HTML + embedded Next.js pageProps is the primary format; DOM fallback is explicit.
No attachment downloads, Telegram calls, historical crawling or persistent state.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote
from urllib.robotparser import RobotFileParser
from uuid import UUID

from bs4 import BeautifulSoup
import requests

BASE = 'https://kemdiktisaintek.go.id'
LIST = BASE + '/announcement'
ARTICLE = '/announcement/article/'
USER_AGENT = 'DiktiNews-Stage1-Parser/1.0'
WIB = timezone(timedelta(hours=7))
MONTHS = 'januari februari maret april mei juni juli agustus september oktober november desember'.split()
DATE = r'\d{1,2}\s+(?:' + '|'.join(MONTHS) + r')\s+\d{4}'
DATE_TIME = re.compile(DATE + r'(?:\s*\|\s*\d{2}[.:]\d{2}\s*WIB)?', re.I)


class ParseError(ValueError):
    """Unrecognized, incomplete or conflicting source structure."""


class FetchError(RuntimeError):
    """Safe network error; contains no response body or credential-bearing URL."""


def text(value):
    return ' '.join((value or '').split())


def normalize_url(value, base=LIST, *, article=False):
    if not isinstance(value, str) or not value.strip() or re.search(r'[\x00-\x20\\]', value.strip()):
        raise ParseError('INVALID_URL')
    try:
        parts = urlsplit(urljoin(base, value.strip()))
        port = parts.port
    except ValueError:
        raise ParseError('INVALID_URL') from None
    if (parts.scheme not in {'http', 'https'} or not parts.hostname
            or parts.username or parts.password or port not in {None, 80, 443}):
        raise ParseError('INVALID_URL')
    host = parts.hostname.lower()
    path = parts.path or '/'
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    scheme = parts.scheme
    if article:
        if host != urlsplit(BASE).hostname or not re.fullmatch(ARTICLE + r'[a-zA-Z0-9_%\-]+/?', path):
            raise ParseError('INVALID_ARTICLE_URL')
        path, query, scheme = path.rstrip('/'), [], 'https'
    return urlunsplit((scheme, host, path, urlencode(query), ''))


def normalize_date(value):
    """ISO timestamps require an explicit offset; Indonesian date labels use WIB.

    Date-only labels remain date-only, avoiding an invented publication time.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    value = text(value)
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        if 'T' in value:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.astimezone(WIB).isoformat() if stamp.tzinfo else None
        match = re.fullmatch(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:\s*\|\s*(\d{2})[.:](\d{2})\s*WIB)?', value, re.I)
        if not match:
            return None
        day, month, year, hour, minute = match.groups()
        stamp = datetime(int(year), MONTHS.index(month.lower()) + 1, int(day),
                         int(hour or 0), int(minute or 0), tzinfo=WIB)
        return stamp.isoformat() if hour is not None else stamp.date().isoformat()
    except (ValueError, OverflowError):
        return None


def _soup(html):
    if not html or not html.strip():
        raise ParseError('EMPTY_RESPONSE')
    return BeautifulSoup(html, 'html.parser')


def _props(soup):
    tag = soup.select_one('script#__NEXT_DATA__')
    if tag is None:
        return None
    try:
        props = json.loads(tag.string or '')['props']['pageProps']
        if not isinstance(props, dict):
            raise ValueError()
        return props
    except (KeyError, TypeError, ValueError):
        raise ParseError('INVALID_EMBEDDED_JSON') from None


def _item(row):
    if not isinstance(row, dict):
        raise ParseError('INVALID_ITEM')
    title, slug = row.get('title'), row.get('slug')
    if not isinstance(title, str) or not text(title) or not isinstance(slug, str) or not slug:
        raise ParseError('MISSING_TITLE_OR_SLUG')
    url = normalize_url(ARTICLE + slug, article=True)
    identifier = row.get('id')
    if identifier is not None:
        try:
            identifier = str(UUID(identifier))
        except (ValueError, TypeError, AttributeError):
            raise ParseError('INVALID_UUID') from None
    category = row.get('category')
    if category is not None and not isinstance(category, str):
        raise ParseError('INVALID_CATEGORY')
    return {'source': 'kemdiktisaintek', 'id': identifier,
            'identity': 'kemdiktisaintek:' + (identifier or 'url:' + url),
            'identity_basis': 'uuid' if identifier else 'url', 'slug': slug,
            'title': text(title), 'category': text(category) or None, 'url': url,
            'created_at': normalize_date(row.get('created_at')),
            'published_at': None, 'updated_at': None,
            'warnings': [] if identifier else ['UUID_UNAVAILABLE']}


def _dedupe(items):
    urls, ids, result = {}, {}, []
    for item in items:
        old = urls.get(item['url']) or ids.get(item['identity'])
        if old:
            if any(old.get(k) != item.get(k) for k in ('title', 'url', 'identity')):
                raise ParseError('CONFLICTING_DUPLICATE')
            continue
        urls[item['url']] = ids[item['identity']] = item
        result.append(item)
    return result


def _next(soup, page_url):
    for anchor in soup.select('a[href]'):
        if text(anchor.get_text()).casefold() not in {'next', 'selanjutnya'} and 'next' not in anchor.get('rel', []):
            continue
        url = normalize_url(anchor['href'], page_url)
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        current = dict(parse_qsl(urlsplit(page_url).query)).get('page', '1')
        if (parts.hostname != urlsplit(BASE).hostname or parts.path != '/announcement'
                or not query.get('page', '').isdigit() or int(query['page']) != int(current) + 1):
            raise ParseError('INVALID_PAGINATION')
        return url
    return None


def parse_list(html, page_url=LIST):
    soup = _soup(html)
    props = _props(soup)
    if props is not None:
        data = props.get('announcements')
        if (not isinstance(data, dict) or not isinstance(data.get('data'), list)
                or type(data.get('total')) is not int or data['total'] < len(data['data'])):
            raise ParseError('INVALID_LIST_SCHEMA')
        items = []
        for row in data['data']:
            item = _item(row)
            # On the observed listing updated_at supplies the visible date, NOT
            # a trustworthy modification timestamp (detail has a separate value).
            item['published_at'] = normalize_date(row.get('updated_at'))
            item['publication_basis'] = 'listing_display_timestamp'
            if item['published_at'] is None:
                item['warnings'].append('PUBLICATION_DATE_UNAVAILABLE')
            items.append(item)
        total, method = data['total'], 'html_embedded_json'
    else:
        h1 = soup.find('h1')
        if h1 is None or text(h1.get_text()).casefold() != 'pengumuman':
            raise ParseError('LIST_STRUCTURE_CHANGED')
        items = []
        for anchor in soup.select('a[href*="/announcement/article/"]'):
            if text(anchor.get_text()).casefold() in {'lihat pengumuman', ''}:
                continue
            url = normalize_url(anchor['href'], page_url, article=True)
            card = anchor.parent
            label = DATE_TIME.search(card.get_text(' ', strip=True))
            category = card.select_one('p.text-blue-400')
            item = _item({'slug': urlsplit(url).path.split('/')[-1], 'title': anchor.get_text(),
                          'category': category.get_text() if category else None})
            item.update(published_at=normalize_date(label.group()) if label else None,
                        publication_basis='dom_label')
            item['warnings'].append('DOM_FALLBACK')
            if not label:
                item['warnings'].append('PUBLICATION_DATE_UNAVAILABLE')
            items.append(item)
        total, method = None, 'html_dom'
        if not items:
            # No generic "no results" guessing: absence without JSON is unhealthy.
            raise ParseError('LIST_STRUCTURE_CHANGED_OR_EMPTY')
    return {'items': _dedupe(items), 'total': total, 'next_url': _next(soup, page_url),
            'method': method, 'url': page_url}


def parse_api_list(body, page_url):
    """Verified public /api/announcement response. No build ID dependency."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        raise ParseError('INVALID_API_JSON') from None
    if (not isinstance(data, dict) or not isinstance(data.get('data'), list)
            or type(data.get('total')) is not int or data['total'] < len(data['data'])):
        raise ParseError('INVALID_API_SCHEMA')
    parts = urlsplit(page_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    try:
        page, size = int(params.get('page', '1')), int(params.get('size', '10'))
        if page < 1 or not 1 <= size <= 10:
            raise ValueError()
    except ValueError:
        raise ParseError('INVALID_API_PAGINATION') from None
    if not data['data'] and data['total'] > (page - 1) * size:
        raise ParseError('INCOMPLETE_API_PAGE')
    items = []
    for row in data['data']:
        item = _item(row)
        item.update(published_at=normalize_date(row.get('updated_at')),
                    publication_basis='listing_display_timestamp')
        if item['published_at'] is None:
            item['warnings'].append('PUBLICATION_DATE_UNAVAILABLE')
        items.append(item)
    next_url = None
    if page * size < data['total']:
        params.update(page=page + 1, size=size)
        next_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))
    return {'items': _dedupe(items), 'total': data['total'], 'next_url': next_url,
            'method': 'public_json_api', 'url': page_url}


def _links(container, article_url, source=None):
    candidates = [(a.get('href'), text(a.get_text())) for a in container.select('a[href]')]
    candidates += [(a.get('src') or a.get('data'), 'Embedded document')
                   for a in container.select('iframe[src], embed[src], object[data]')]
    if source:
        candidates.append((source, 'Lihat Pengumuman Selengkapnya'))
    attachments, external, seen = [], [], set()
    for raw, label in candidates:
        try:
            url = normalize_url(raw, article_url)
        except ParseError:
            continue  # mailto/javascript/data are never document URLs
        if url in seen:
            continue
        seen.add(url)
        parts = urlsplit(url)
        suffix = unquote(parts.path).lower()
        kind = ('pdf' if suffix.endswith('.pdf') else 'document' if
                re.search(r'\.(docx?|xlsx?|pptx?|zip)$', suffix) or parts.path.startswith('/api/file/')
                else 'external_document' if parts.hostname in {'drive.google.com', 'docs.google.com'} else None)
        row = {'url': url, 'label': label or unquote(parts.path.rsplit('/', 1)[-1])}
        if kind:
            attachments.append({**row, 'kind': kind, 'verification': 'link_only'})
        elif parts.hostname != urlsplit(BASE).hostname:
            external.append(row)
    return attachments, external


def parse_detail(html, article_url, listing=None):
    article_url = normalize_url(article_url, article=True)
    soup = _soup(html)
    props = _props(soup)
    h1, article = soup.find('h1'), soup.select_one('article.rich-content')
    if h1 is None or article is None or not text(h1.get_text()):
        raise ParseError('DETAIL_STRUCTURE_CHANGED')
    container = h1.parent
    if article not in container.descendants:
        raise ParseError('DETAIL_SCOPE_CHANGED')
    if props is not None:
        row = props.get('data')
        item = _item(row)
        if item['url'] != article_url or item['title'] != text(h1.get_text()):
            raise ParseError('DETAIL_IDENTITY_CONFLICT')
        item.update(published_at=normalize_date(row.get('datetime')),
                    publication_basis='detail_datetime', updated_at=normalize_date(row.get('updated_at')))
        source, method = row.get('source'), 'html_embedded_json_and_dom'
    else:
        item = _item({'slug': urlsplit(article_url).path.split('/')[-1], 'title': h1.get_text()})
        label = DATE_TIME.search(container.get_text(' ', strip=True))
        item.update(published_at=normalize_date(label.group()) if label else None,
                    publication_basis='dom_label')
        item['warnings'].append('DOM_FALLBACK')
        source, method = None, 'html_dom'
    if listing:
        if (listing['url'] != item['url'] or (listing.get('id') and item.get('id')
                                             and listing['id'] != item['id'])):
            raise ParseError('LIST_DETAIL_IDENTITY_CONFLICT')
        item['category'] = item['category'] or listing.get('category')
    attachments, external = _links(container, article_url, source)
    for tag in article.select('script, style, noscript'):
        tag.decompose()
    body = text(article.get_text(' ', strip=True))
    if not body:
        item['warnings'].append('EMPTY_ARTICLE_BODY')
    if not item['published_at']:
        item['warnings'].append('PUBLICATION_DATE_UNAVAILABLE')
    letter = re.search(r'\b(?:nomor(?:\s+surat)?|no\.?\s*surat)\s*:\s*([\w./-]+/\d{4})\b', body, re.I)
    deadlines = [text(m.group()) for m in re.finditer(
        r'(?:batas\s+pendaftaran|tenggat(?:\s+waktu)?|paling\s+lambat|hingga|sampai\s+dengan)'
        r'\s*:?\s*' + DATE + r'(?:,?\s*(?:pukul\s*)?\d{1,2}[.:]\d{2}\s*WIB)?', body, re.I)]
    item.update(body_text=body, letter_number=letter.group(1) if letter else None,
                deadline_evidence=deadlines, attachments=attachments, external_links=external,
                method=method)
    # Content fingerprint excludes clocks, changing markup, source timestamps,
    # and embedded image data. It is evidence for later review, never a send flag.
    payload = {k: item[k] for k in ('title', 'category', 'body_text', 'attachments', 'external_links')}
    item['content_sha256'] = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return item


class AnnouncementClient:
    """GET-only fixed-origin client; at most two attempts, paced, no redirects.

    robots 404 means no published rules (reported as such); robots network,
    forbidden access, redirects and malformed HTML-as-rules fail closed.
    """
    def __init__(self, *, session=None, sleep=time.sleep, monotonic=time.monotonic, interval=1.0):
        if interval < 1:
            raise ValueError('Minimum request interval is one second')
        self.session = session or requests.Session()
        self.sleep, self.monotonic, self.interval = sleep, monotonic, interval
        self.last_request = None
        self.robots = None
        self.robots_status = 'unchecked'

    def _get(self, url, *, robots=False):
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.netloc != urlsplit(BASE).netloc
                or (not robots and parts.path not in {'/announcement', '/api/announcement'}
                    and not parts.path.startswith(ARTICLE))):
            raise FetchError('REQUEST_OUTSIDE_SOURCE_SCOPE')
        for attempt in range(2):
            if self.last_request is not None:
                self.sleep(max(0, self.interval - (self.monotonic() - self.last_request)))
            self.last_request = self.monotonic()
            try:
                response = self.session.get(url, timeout=(10, 30), allow_redirects=False,
                                            headers={'User-Agent': USER_AGENT})
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 0:
                    self.sleep(2)
                    continue
                raise FetchError('NETWORK_TIMEOUT_OR_CONNECTION_FAILURE') from None
            except requests.RequestException:
                raise FetchError('NETWORK_FAILURE') from None
            if robots and response.status_code == 404:
                return None
            if response.status_code in {500, 502, 503, 504} and attempt == 0:
                self.sleep(2)
                continue
            if response.status_code != 200:
                raise FetchError(f'HTTP_{response.status_code}')
            if len(response.content) > 5_000_000:
                raise FetchError('RESPONSE_TOO_LARGE')
            # Explicit UTF-8: requests guesses ISO-8859-1 on some text responses.
            try:
                body = response.content.decode('utf-8-sig', errors='strict')
            except UnicodeDecodeError:
                raise FetchError('INVALID_RESPONSE_ENCODING') from None
            if not body.strip():
                raise FetchError('EMPTY_RESPONSE')
            expected_type = 'application/json' if parts.path == '/api/announcement' else 'text/html'
            if not robots and expected_type not in response.headers.get('Content-Type', '').lower():
                raise FetchError('UNEXPECTED_CONTENT_TYPE')
            return body

    def check_robots(self):
        body = self._get(BASE + '/robots.txt', robots=True)
        rules = RobotFileParser()
        if body is None:
            rules.parse([])
            self.robots_status = 'not_found_404'
        else:
            if '<html' in body.lower() or 'user-agent:' not in body.lower():
                raise FetchError('ROBOTS_UNRECOGNIZED')
            rules.parse(body.splitlines())
            self.robots_status = 'rules_loaded'
            self.interval = max(self.interval, rules.crawl_delay(USER_AGENT) or 0)
            rate = rules.request_rate(USER_AGENT)
            if rate:
                self.interval = max(self.interval, rate.seconds / rate.requests)
        self.robots = rules

    def fetch(self, url):
        if self.robots is None:
            self.check_robots()
        if not self.robots.can_fetch(USER_AGENT, url):
            raise FetchError('ROBOTS_DISALLOWED')
        return self._get(url)

    def collect(self, *, max_pages=1, detail_limit=3, search='', category=None, method='html'):
        if type(max_pages) is not int or not 1 <= max_pages <= 3 or type(detail_limit) is not int or not 0 <= detail_limit <= 10:
            raise ValueError('Bounded sampling requires 1..3 pages and 0..10 details')
        if category not in {None, 'umum', 'pendidikan-tinggi', 'riset-dan-pengembangan', 'sains-dan-teknologi'}:
            raise ValueError('Unknown category')
        if method not in {'html', 'api'}:
            raise ValueError('Unknown retrieval method')
        params = {'page': 1, 'size': 10, 'search': search}
        if category:
            params['category'] = category
        endpoint = BASE + '/api/announcement' if method == 'api' else LIST
        url, pages, items = endpoint + '?' + urlencode(params), [], []
        for _ in range(max_pages):
            page = (parse_api_list if method == 'api' else parse_list)(self.fetch(url), url)
            pages.append({k: page[k] for k in ('url', 'method', 'total', 'next_url')})
            items.extend(page['items'])
            if not page['next_url']:
                break
            url = page['next_url']
        items = _dedupe(items)
        for index, item in enumerate(items[:detail_limit]):
            items[index] = parse_detail(self.fetch(item['url']), item['url'], item)
        return {'schema_version': 1, 'source': 'kemdiktisaintek',
                'observed_at': datetime.now(timezone.utc).isoformat(),
                'delivery_authorized': False, 'robots_status': self.robots_status,
                'pages': pages, 'items': items,
                'bounded': True, 'archive_complete': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages', type=int, default=1)
    parser.add_argument('--details', type=int, default=3)
    parser.add_argument('--search', default='')
    parser.add_argument('--category')
    parser.add_argument('--method', choices=['html', 'api'], default='html')
    args = parser.parse_args()
    client = AnnouncementClient()
    try:
        result = client.collect(max_pages=args.pages, detail_limit=args.details,
                                search=args.search, category=args.category, method=args.method)
    except (FetchError, ParseError, ValueError) as exc:
        print(json.dumps({'source': 'kemdiktisaintek', 'delivery_authorized': False,
                          'status': 'FAILED', 'error': str(exc)}))
        return 1
    finally:
        client.session.close()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
