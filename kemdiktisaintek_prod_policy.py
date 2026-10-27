"""Legacy relevance diagnostics only; official-source delivery has no topic filter.

Keep the diagnostic version stable so historic observation records remain valid.
Admission/eligibility is versioned separately by the explicit delivery migration.
"""
from datetime import datetime
import re
import unicodedata

POLICY_VERSION = 'kemdiktisaintek-production-v1'


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
