"""Offline rendering regressions from the five delivered multi-source previews."""
from copy import deepcopy
from datetime import datetime
from html import escape
import json
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import pytest

import notifier as notifier_module
from notifier import Notifier, _format_bima_date, _format_youtube_description
from monitor_runner import event_key, item_key
from kemdiktisaintek_prod_delivery_state import event_key as announcement_event_key
from kemdiktisaintek_prod_telegram import format_announcement, document_filename


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/telegram_ux_scenarios.json').read_text(encoding='utf-8'))
FOOTER = '━━━━━━━━━━━━━━━━━━━━\n<i>Bot Dikti News</i>'


def render_scenarios(monkeypatch, inputs):
    stamp = datetime.fromisoformat(FIXTURE['provenance']['rendered_at']).astimezone(notifier_module.WIB)
    monkeypatch.setattr(notifier_module, 'now_wib', lambda: stamp)
    notifier = Notifier(config={'notification_method': 'console'})
    groups = {'A': inputs['bima'], 'B': inputs['youtube'],
              'C': [inputs['bima'][0], inputs['youtube'][0]]}
    output = {}
    for name, items in groups.items():
        chunks = notifier._build_summary_chunks(items)
        assert len(chunks) == 1
        output[name] = chunks[0]
    output.update({name: format_announcement(item)['text']
                   for name, item in zip('DE', inputs['kemdiktisaintek'])})
    return output


@pytest.mark.parametrize('english,indonesian', zip(
    'January February March April May June July August September October November December'.split(),
    'Januari Februari Maret April Mei Juni Juli Agustus September Oktober November Desember'.split(),
))
def test_english_bima_months_are_rendered_in_indonesian(english, indonesian):
    assert _format_bima_date('26 ' + english + ' 2026') == '26 ' + indonesian + ' 2026'


@pytest.mark.parametrize('raw', [
    '31 April 2026', '29 February 2025', '32 August 2026', '0 August 2026',
    '26 August 0000', '26 Unknown 2026', '26 Agustus 2026', '2026-08-26',
    '26 August 2026 12:00 WIB', '<unrecognized & date>', '',
])
def test_unrecognized_or_invalid_bima_date_remains_source_text(raw):
    assert _format_bima_date(raw) == raw


def test_valid_leap_date_case_whitespace_and_leading_zero():
    assert _format_bima_date('29 February 2024') == '29 Februari 2024'
    assert _format_bima_date(' 05  MARCH 2026 ') == '05 Maret 2026'


@pytest.mark.parametrize('raw,expected', [
    ('Deskripsi pendek.', 'Deskripsi pendek.'),
    (' \tRingkasan\n\n  pendek.\r\n', 'Ringkasan pendek.'),
    ('Satu\u00a0dua\t tiga', 'Satu dua tiga'),
    ('   \n\t', ''), (None, ''), ('x' * 180, 'x' * 180),
])
def test_short_description_is_whitespace_normalized_without_added_ellipsis(raw, expected):
    assert _format_youtube_description(raw) == expected


def test_long_description_ends_at_word_boundary_within_180_characters():
    raw = 'Informasi layanan pendidikan tinggi untuk dosen dan peneliti ' * 8
    text = ' '.join(raw.split())
    short = _format_youtube_description(raw)
    assert len(short) <= 180 and short.endswith('...')
    assert text.startswith(short[:-3])
    assert text[len(short[:-3])].isspace()


def test_single_oversized_word_uses_bounded_fallback():
    assert _format_youtube_description('x' * 181) == 'x' * 177 + '...'


@pytest.mark.parametrize('scenario', list('ABCDE'))
def test_delivered_preview_sources_match_final_html_snapshots(monkeypatch, scenario):
    messages = render_scenarios(monkeypatch, deepcopy(FIXTURE['inputs']))
    assert messages[scenario] == FIXTURE['expected_html'][scenario]
    assert messages[scenario].endswith(FOOTER)
    assert len(messages[scenario].encode('utf-16-le')) // 2 <= 3900
    root = ET.fromstring('<root>' + messages[scenario] + '</root>')
    assert all(node.tag in {'root', 'b', 'i', 'a'} for node in root.iter())
    for link in root.iter('a'):
        assert urlsplit(link.attrib['href']).scheme == 'https'
    assert 'UJI TAMPILAN' not in messages[scenario]


def test_mixed_header_counts_and_numbering_are_preserved(monkeypatch):
    messages = render_scenarios(monkeypatch, deepcopy(FIXTURE['inputs']))
    mixed = messages['C']
    assert mixed.startswith('<b>📢 UPDATE BARU BIMA + YOUTUBE KEMDIKTISAINTEK</b>\n\n')
    assert '🔢 Jumlah: 2 item baru\n🏛️ BIMA: 1\n📺 YouTube: 1\n' in mixed
    assert '<b>1. ' + escape(FIXTURE['inputs']['bima'][0]['title']) + '</b>' in mixed
    assert '<b>2. ' + escape(FIXTURE['inputs']['youtube'][0]['title']) + '</b>' in mixed
    assert '26 Agustus 2026' in messages['A']
    assert '26 August 2026' not in messages['A']
    assert 'Sosialisasi Buku Panduan Kredensial Mikro...\n' not in messages['B']


def test_rendering_preserves_source_identity_metadata_urls_and_previous_receipts(monkeypatch):
    inputs = deepcopy(FIXTURE['inputs'])
    before = deepcopy(inputs)
    receipts = deepcopy(FIXTURE['preview_receipts'])
    records = inputs['bima'] + inputs['youtube']
    old_keys = [(item_key(item), event_key(item)) for item in records]
    announcement_keys = [announcement_event_key(item['id']) for item in inputs['kemdiktisaintek']]
    messages = render_scenarios(monkeypatch, inputs)
    assert inputs == before
    assert old_keys == [(item_key(item), event_key(item)) for item in records]
    assert announcement_keys == [announcement_event_key(item['id']) for item in inputs['kemdiktisaintek']]
    assert FIXTURE['preview_receipts'] == receipts
    assert [row['message_id'] for row in receipts] == list(range(40, 47))
    for item in records:
        assert any(escape(item['url']) in text for text in messages.values())
    assert inputs['bima'][1]['date'] == '26 August 2026'


@pytest.mark.parametrize('scenario,index', [('D', 0), ('E', 1)])
def test_kemdiktisaintek_icons_dates_optional_fields_and_complete_title(monkeypatch, scenario, index):
    inputs = deepcopy(FIXTURE['inputs'])
    message = render_scenarios(monkeypatch, inputs)[scenario]
    item = inputs['kemdiktisaintek'][index]
    assert message.startswith('<b>📢 PENGUMUMAN BARU KEMDIKTISAINTEK</b>\n\n📅 ')
    assert '<b>1. ' + escape(item['title']) + '</b>' in message
    assert '📦 Dokumen: 1 file' in message
    doc = item['attachments'][0]
    assert '\n📎 ' + escape(document_filename(doc['url'], doc['label'])) in message
    assert '\n\n🔗 Sumber:\n' + item['url'] in message
    assert 'Kategori:' not in message and 'Ringkasan:' not in message
    assert ('⏳ Tenggat:' in message) == (scenario == 'D')


def test_optional_letter_deadline_and_html_escaping_are_only_rendering():
    item = deepcopy(FIXTURE['inputs']['kemdiktisaintek'][0])
    item.update(title='Hibah <Riset> & Mitra', letter_number='3414/DT.2.1/2026')
    before = deepcopy(item)
    text = format_announcement(item)['text']
    assert '<b>1. Hibah &lt;Riset&gt; &amp; Mitra</b>\n   📜 3414/DT.2.1/2026' in text
    assert '\n   ⏳ Tenggat: paling lambat 01 Maret 2027' in text
    ET.fromstring('<root>' + text + '</root>')
    assert item == before


def test_youtube_description_escaping_and_empty_whitespace():
    item = deepcopy(FIXTURE['inputs']['youtube'][0])
    item['description'] = 'Riset <aman>\n &\t jelas'
    notifier = Notifier(config={'notification_method': 'console'})
    text = notifier._format_html_item(1, item)
    assert '📝 Riset &lt;aman&gt; &amp; jelas\n' in text
    assert '...' not in text
    item['description'] = ' \n\t '
    assert '📝' not in notifier._format_html_item(1, item)
