"""Explainable relevance and conservative cross-source matching; pure functions."""
from datetime import datetime
import re
import unicodedata

from kemdiktisaintek_parser import normalize_date, normalize_url, ParseError

POLICY_VERSION = 'kemdiktisaintek-experiment-v1'


def normalized(value):
    return ' '.join(re.sub(r'[^\w\s]', ' ', unicodedata.normalize('NFKC', value or '').casefold()).split())


def classify(item):
    title, body = normalized(item.get('title')), normalized(item.get('body_text'))
    full = title + ' ' + body
    evidence = {'title': bool(title), 'body': bool(body), 'category': item.get('category'),
                'published_at': item.get('published_at'), 'deadline': bool(item.get('deadline_evidence')),
                'letter': bool(item.get('letter_number')), 'attachments': bool(item.get('attachments'))}
    reasons = []
    high = [phrase for phrase in ('hibah penelitian', 'pengabdian kepada masyarakat', 'pendanaan riset',
        'beasiswa dosen', 'program doktor', 'akreditasi perguruan tinggi', 'jabatan akademik dosen',
        'regulasi pendidikan tinggi') if phrase in full]
    if 'reviewer' in full and any(p in full for p in ('beasiswa', 'penelitian', 'riset', 'pddi')):
        high.append('reviewer akademik')
    academic = any(p in full for p in ('dosen', 'peneliti', 'mahasiswa', 'perguruan tinggi', 'sivitas', 'ilmuwan'))
    action = any(p in full for p in ('pendaftaran', 'penawaran', 'seleksi', 'batas', 'dibuka', 'penetapan', 'ketentuan'))
    medium = [p for p in ('seminar ilmiah', 'kompetisi mahasiswa', 'pelatihan', 'webinar',
                          'pengembangan kompetensi') if p in full]
    nonacademic = ('tata usaha' in title or 'wakil kepala sekolah' in title) and any(
        p in title for p in ('sekolah', 'sma', 'guru'))
    ceremony = any(p in title for p in ('dibuka di', 'digelar di', 'peresmian', 'menghadiri', 'laporan kegiatan'))
    if nonacademic:
        decision, priority, reasons = 'NOT_RELEVANT', 'low', ['NONACADEMIC_SCHOOL_RECRUITMENT']
    elif ceremony and not item.get('deadline_evidence') and not high:
        decision, priority, reasons = 'NOT_RELEVANT', 'low', ['COMPLETED_OR_CEREMONIAL_REPORT']
    elif high and (academic or action) and (body or evidence['attachments'] or evidence['deadline']):
        decision, priority, reasons = 'RELEVANT', 'high', ['PRIORITY_TOPIC:' + p for p in high]
    elif academic and action and evidence['deadline'] and (body or evidence['attachments']):
        decision, priority, reasons = 'RELEVANT', 'high', ['ACADEMIC_ACTION_WITH_EXPLICIT_DEADLINE']
    elif medium and academic and (body or evidence['attachments']):
        decision, priority, reasons = 'RELEVANT', 'medium', ['ACADEMIC_DEVELOPMENT:' + p for p in medium]
    else:
        decision, priority, reasons = 'NEEDS_REVIEW', 'unknown', ['INSUFFICIENT_COMBINED_EVIDENCE']
    return {'decision': decision, 'priority': priority, 'reasons': reasons,
            'evidence': evidence, 'policy_version': POLICY_VERSION}


def _day(value):
    stamp = normalize_date(value)
    if stamp:
        return stamp[:10]
    return None


def _documents(item, bima=False):
    values = item.get('documents' if bima else 'attachments', [])
    result = set()
    for doc in values:
        if not isinstance(doc, dict):
            continue
        raw = doc.get('url') or doc.get('download_url') or doc.get('api_url')
        if raw:
            try:
                result.add(normalize_url(raw))
            except ParseError:
                pass
    return result


def cross_source_dedup(item, history):
    """Read-only evidence. Letter/title similarity alone never declares a duplicate."""
    if history is None:
        return {'status': 'NEEDS_REVIEW', 'matches': [], 'reason': 'BIMA_REFERENCE_MISSING'}
    if not isinstance(history, list) or any(not isinstance(row, dict) or not isinstance(row.get('title'), str)
                                           for row in history):
        raise ValueError('INVALID_BIMA_REFERENCE')
    letter = normalized(item.get('letter_number')).replace(' ', '')
    title = normalized(item.get('title'))
    docs, matches = _documents(item), []
    for index, row in enumerate(history):
        old_title = normalized(row.get('title'))
        old_letter = normalized(row.get('surat')).replace(' ', '')
        same_letter = bool(letter and old_letter and letter == old_letter)
        same_title = bool(title and title == old_title)
        same_day = bool(_day(item.get('published_at')) and
                        _day(item.get('published_at')) == _day(row.get('date')))
        same_doc = bool(docs & _documents(row, bima=True))
        words, old_words = set(title.split()), set(old_title.split())
        similar = bool(words and old_words and len(words & old_words) / len(words | old_words) >= .65)
        program = normalized(item.get('program_name'))
        same_program = bool(program and program == normalized(row.get('program_name')))
        evidence = [name for name, yes in [('letter', same_letter), ('title', same_title),
            ('publication_day', same_day), ('document_url', same_doc), ('program', same_program)] if yes]
        duplicate = same_title and (same_doc or (same_letter and same_day))
        if duplicate or same_letter or same_title or same_doc or (similar and (same_day or same_program)):
            matches.append({'bima_index': index, 'status': 'DUPLICATE' if duplicate else 'NEEDS_REVIEW',
                            'evidence': evidence, 'title_similarity_only': similar and not same_title})
    status = 'DUPLICATE' if any(m['status'] == 'DUPLICATE' for m in matches) else 'NEEDS_REVIEW' if matches else 'NO_MATCH'
    return {'status': status, 'matches': matches, 'policy_version': POLICY_VERSION}
