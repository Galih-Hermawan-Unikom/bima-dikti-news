"""Offline fixtures + injected HTTP; never use real networking in tests."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

from bs4 import BeautifulSoup
import pytest
import requests

from kemdiktisaintek_parser import (AnnouncementClient, BASE, LIST, FetchError, ParseError,
                                    normalize_date, normalize_url, parse_detail, parse_list, parse_api_list)

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Real network forbidden in prototype tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)


def html(name='list.html'):
    return (FIXTURES / name).read_text(encoding='utf-8')


def mutate(name, change):
    soup = BeautifulSoup(html(name), 'html.parser')
    tag = soup.select_one('#__NEXT_DATA__')
    data = json.loads(tag.string)
    change(data['props']['pageProps'])
    tag.string = json.dumps(data)
    return str(soup)


def response(body='', status=200, content_type='text/html; charset=utf-8'):
    return Mock(status_code=status, content=body.encode(), headers={'Content-Type': content_type})


def client(*responses):
    session = Mock()
    session.get.side_effect = responses
    instance = AnnouncementClient(session=session, sleep=Mock(), monotonic=lambda: 0)
    return instance, session


def test_real_listing_and_pagination():
    page = parse_list(html())
    assert len(page['items']) == 10 and page['total'] == 928
    assert page['items'][0]['id'] == '72b0b13a-ed5b-47c2-b159-4336970369d4'
    assert page['items'][0]['published_at'] == '2026-10-20T05:00:00+07:00'
    assert page['items'][0]['updated_at'] is None
    assert 'page=2' in page['next_url']
    page2 = parse_list(html('page2.html'), LIST + '?page=2&size=10')
    assert 'page=3' in page2['next_url']
    assert page2['items'][0]['url'] != page['items'][0]['url']


@pytest.mark.parametrize('name,total,category', [('category.html', 0, None),
    ('higher.html', 23, 'Pendidikan-tinggi'), ('science.html', 22, 'Sains-dan-teknologi'),
    ('general.html', 883, 'Umum')])
def test_categories(name, total, category):
    page = parse_list(html(name))
    assert page['total'] == total
    if category:
        assert all(i['category'] == category for i in page['items'])
    else:
        assert page['items'] == [] and page['next_url'] is None


def test_search_fixture():
    page = parse_list(html('search.html'), LIST + '?search=beasiswa')
    assert all('beasiswa' in i['title'].lower() for i in page['items'])
    assert 'search=beasiswa' in page['next_url']


@pytest.mark.parametrize('name,index', [('detail0.html', 0), ('detail2.html', 2), ('detail7.html', 7)])
def test_real_details(name, index):
    listing = parse_list(html())['items'][index]
    item = parse_detail(html(name), listing['url'], listing)
    assert item['id'] == listing['id'] and item['category']
    assert item['body_text'] and '<p>' not in item['body_text']
    assert any(a['kind'] == 'pdf' for a in item['attachments'])
    assert len(item['content_sha256']) == 64
    assert item['updated_at'] and item['published_at']
    assert item['identity'] == 'kemdiktisaintek:' + item['id']


def test_external_documents_and_deadline():
    listing = parse_list(html())['items']
    item = parse_detail(html('detail0.html'), listing[0]['url'])
    assert any(a['kind'] == 'external_document' for a in item['attachments'])
    item = parse_detail(html('detail7.html'), listing[7]['url'])
    assert item['deadline_evidence'] == ['Batas pendaftaran: 25 September 2026, pukul 23.59 WIB']
    assert item['external_links'][0]['url'] == 'https://bit.ly/RegistrasiLNLM76'
    assert item['letter_number'] is None  # never invent from the PDF filename


@pytest.mark.parametrize('raw,expected', [
    ('20 Oktober 2026 | 05.00 WIB', '2026-10-20T05:00:00+07:00'),
    ('16 september 2026 | 14:00 WIB', '2026-09-16T14:00:00+07:00'),
    ('2026-09-16T07:00:00.000Z', '2026-09-16T14:00:00+07:00'),
    ('1 Januari 2026', '2026-01-01'), ('2026-02-28', '2026-02-28'),
    ('31 Februari 2026', None), ('2026-01-01T00:00:00', None), ('invalid', None),
    ('1 Oktober 2026 | 25.00 WIB', None), ('', None), (None, None)])
def test_dates(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize('url', ['/announcement/article/abc/',
    BASE.upper() + '/announcement/article/abc?utm_source=test#x',
    '//kemdiktisaintek.go.id/announcement/article/abc?foo=1'])
def test_article_url(url):
    assert normalize_url(url, article=True) == BASE + '/announcement/article/abc'


def test_document_url_preserves_nontracking_query():
    assert normalize_url('/file.pdf?token=abc&utm_source=x#page=2') == BASE + '/file.pdf?token=abc'


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,x',
    'https://evil.invalid/announcement/article/x',
    'https://kemdiktisaintek.go.id:444/announcement/article/x',
    'https://user:secret@kemdiktisaintek.go.id/announcement/article/x', '', '/announcement/article/a/b'])
def test_reject_bad_article_urls(url):
    with pytest.raises(ParseError):
        normalize_url(url, article=True)


def test_dom_fallback():
    soup = BeautifulSoup(html(), 'html.parser')
    soup.select_one('#__NEXT_DATA__').decompose()
    page = parse_list(str(soup))
    assert len(page['items']) == 10
    assert page['items'][0]['published_at'] == '2026-10-20T05:00:00+07:00'
    assert page['items'][0]['identity_basis'] == 'url'
    assert page['items'][0]['category'] == 'Sains-dan-teknologi'


def test_dom_detail_without_attachment_and_letter():
    body = '<div><h1>Fixture</h1><p>1 Januari 2026 | 01.00 WIB</p><article class="rich-content"><p>Nomor Surat: 123/ABC/2026</p><script>BAD</script><p>Isi bersih.</p></article></div>'
    item = parse_detail(body, BASE + '/announcement/article/fixture')
    assert item['attachments'] == [] and item['letter_number'] == '123/ABC/2026'
    assert 'BAD' not in item['body_text']


def test_missing_optional_elements():
    item = parse_detail('<div><h1>Fixture</h1><article class="rich-content"></article></div>',
                        BASE + '/announcement/article/fixture')
    assert item['published_at'] is None and item['category'] is None
    assert set(item['warnings']) >= {'PUBLICATION_DATE_UNAVAILABLE', 'EMPTY_ARTICLE_BODY'}


@pytest.mark.parametrize('body', ['', ' ', '<html>maintenance</html>', '<h1>Pengumuman</h1>'])
def test_empty_or_changed_list(body):
    with pytest.raises(ParseError):
        parse_list(body)


@pytest.mark.parametrize('body', ['', '<article>Missing title</article>', '<h1>Missing body</h1>'])
def test_incomplete_detail(body):
    with pytest.raises(ParseError):
        parse_detail(body, BASE + '/announcement/article/fixture')


def test_deduplicate_and_reject_conflict():
    def duplicate(p):
        p['announcements']['data'][1] = copy.deepcopy(p['announcements']['data'][0])
    assert len(parse_list(mutate('list.html', duplicate))['items']) == 9
    def conflict(p):
        duplicate(p)
        p['announcements']['data'][1]['title'] = 'Conflict'
    with pytest.raises(ParseError, match='CONFLICTING_DUPLICATE'):
        parse_list(mutate('list.html', conflict))


def test_bad_json_no_silent_fallback():
    with pytest.raises(ParseError, match='INVALID_EMBEDDED_JSON'):
        parse_list('<h1>Pengumuman</h1><script id="__NEXT_DATA__">oops</script>')


def test_schema_change():
    with pytest.raises(ParseError, match='INVALID_LIST_SCHEMA'):
        parse_list(mutate('list.html', lambda p: p.pop('announcements')))


def test_detail_identity_mismatch():
    with pytest.raises(ParseError, match='IDENTITY_CONFLICT'):
        parse_detail(html('detail0.html'), BASE + '/announcement/article/wrong')


def test_injected_attachment_types():
    body = '<div><h1>Fixture</h1><article class="rich-content"><a href="/a.PDF?download=1">PDF</a><a href="/a.PDF?download=1#x">Duplicate</a><iframe src="/b.pdf"></iframe><a href="https://docs.google.com/document/d/abc">Doc</a><a href="/c.docx">Word</a><a href="javascript:evil">Bad</a></article></div>'
    item = parse_detail(body, BASE + '/announcement/article/fixture')
    assert [a['kind'] for a in item['attachments']] == ['pdf', 'external_document', 'document', 'pdf']


@pytest.mark.parametrize('status', [400, 403, 404, 429, 301, 302])
def test_http_errors_no_retry(status):
    c, s = client(response(status=404), response(status=status))
    with pytest.raises(FetchError, match=f'HTTP_{status}'):
        c.fetch(LIST)
    assert s.get.call_count == 2


def test_timeout_bounded_retry():
    c, s = client(response(status=404), requests.Timeout('SECRET'), requests.Timeout('SECRET'))
    with pytest.raises(FetchError, match='NETWORK_TIMEOUT_OR_CONNECTION_FAILURE') as error:
        c.fetch(LIST)
    assert 'SECRET' not in str(error.value) and s.get.call_count == 3


def test_http_retry_then_success():
    c, s = client(response(status=404), response(status=503), response(html()))
    assert len(parse_list(c.fetch(LIST))['items']) == 10
    assert s.get.call_count == 3
    assert s.get.call_args.kwargs['timeout'] == (10, 30)
    assert s.get.call_args.kwargs['allow_redirects'] is False


@pytest.mark.parametrize('body,content_type,code', [('', 'text/html', 'EMPTY_RESPONSE'),
    ('{}', 'application/json', 'UNEXPECTED_CONTENT_TYPE')])
def test_bad_responses(body, content_type, code):
    c, _ = client(response(status=404), response(body, content_type=content_type))
    with pytest.raises(FetchError, match=code):
        c.fetch(LIST)


def test_robots_deny_and_pacing():
    c, s = client(response('User-agent: *\nDisallow: /announcement\nCrawl-delay: 3', content_type='text/plain'))
    with pytest.raises(FetchError, match='ROBOTS_DISALLOWED'):
        c.fetch(LIST)
    assert s.get.call_count == 1 and c.interval == 3


def test_robots_unavailable_or_html_blocks():
    c, _ = client(response('<html>Not robots</html>'))
    with pytest.raises(FetchError, match='ROBOTS_UNRECOGNIZED'):
        c.fetch(LIST)


def test_client_never_fetches_external_documents():
    c, s = client(response(status=404))
    with pytest.raises(FetchError, match='REQUEST_OUTSIDE_SOURCE_SCOPE'):
        c.fetch('https://drive.google.com/document/test')
    assert s.get.call_count == 1


def test_collect_bounded_with_detail():
    c, s = client(response(status=404), response(html()), response(html('detail0.html')))
    report = c.collect(detail_limit=1)
    assert report['delivery_authorized'] is False and report['archive_complete'] is False
    assert len(report['items']) == 10 and 'body_text' in report['items'][0]
    assert s.get.call_count == 3


@pytest.mark.parametrize('pages,details', [(0, 1), (4, 1), (1, 11), (1, -1), (True, 1)])
def test_reject_unbounded_collection(pages, details):
    c, s = client()
    with pytest.raises(ValueError):
        c.collect(max_pages=pages, detail_limit=details)
    s.get.assert_not_called()


def test_collect_url_dedup_across_pages():
    # Same articles can move/repeat across pages; the next-page link still advances.
    repeated_page2 = html().replace('page=2', 'page=3')
    c, s = client(response(status=404), response(html()), response(repeated_page2))
    report = c.collect(max_pages=2, detail_limit=0)
    assert len(report['items']) == 10 and len(report['pages']) == 2
    assert s.get.call_count == 3


def test_fingerprint_stable_across_markup():
    url = parse_list(html())['items'][7]['url']
    first = parse_detail(html('detail7.html'), url)
    second = parse_detail(html('detail7.html').replace('<article class=', '<article data-test="a" class='), url)
    assert first['content_sha256'] == second['content_sha256']


def test_pagination_loop_rejected():
    with pytest.raises(ParseError, match='INVALID_PAGINATION'):
        parse_list(html(), LIST + '?page=2')


@pytest.mark.parametrize('name', ['api-list.json', 'api-page2.json', 'api-search.json', 'api-category.json'])
def test_api_fixtures(name):
    page = parse_api_list(html(name), BASE + '/api/announcement?page=1&size=10&search=')
    assert len(page['items']) == 10 and 'page=2' in page['next_url']
    assert page['method'] == 'public_json_api'


def test_api_matches_html_exactly():
    api = parse_api_list(html('api-list.json'), BASE + '/api/announcement?page=1&size=10')
    assert api['items'] == parse_list(html())['items']


def test_api_collect_uses_same_detail_parser():
    c, s = client(response(status=404), response(html('api-list.json'), content_type='application/json'),
                  response(html('detail0.html')))
    result = c.collect(method='api', detail_limit=1)
    assert result['pages'][0]['method'] == 'public_json_api'
    assert result['items'][0]['body_text']
    assert s.get.call_count == 3


@pytest.mark.parametrize('body', ['', '{}', '<html/>', '{"data":[],"total":true}', '{"data":[],"total":1}'])
def test_invalid_api_responses(body):
    with pytest.raises(ParseError):
        parse_api_list(body, BASE + '/api/announcement')


def test_empty_api_terminal_page():
    page = parse_api_list('{"data":[],"total":0}', BASE + '/api/announcement')
    assert page['items'] == [] and page['next_url'] is None


@pytest.mark.parametrize('name,list_name', [('detail_higher.html','higher.html'), ('detail_page2.html','page2.html')])
def test_other_category_and_second_page_details(name,list_name):
    item = parse_list(html(list_name), LIST + ('?page=2' if list_name == 'page2.html' else ''))['items'][0]
    result = parse_detail(html(name), item['url'], item)
    assert result['attachments'] and result['body_text'] and result['id'] == item['id']
