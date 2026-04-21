#!/usr/bin/env python3
"""
Council Genius — City of Melton
TechEntity · server.py v10.3

V10.3 deltas vs Melbourne V10.2:
  1. Council-specific constants (contacts, domain, rates model, bin lookup)
  2. Neighbouring-council out-of-area routing tuned to Melton geography
  3. Waste: per-address lookup at www.melton.vic.gov.au/My-Area
  4. Rates: Capital Improved Value (CIV), NOT Net Annual Value
  5. Synonyms file optional (melton_synonyms.json) — server is a no-op if absent
  6. knowledge_meta.json loads PDF manifest; /pdf_lookup serves direct lookups
"""

import os
import json
import csv
import datetime
import hashlib
import time
import re
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote_plus

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_PATH = os.path.join(BASE_DIR, 'knowledge.txt')
ANALYTICS_PATH = os.path.join(BASE_DIR, 'analytics.csv')
FEEDBACK_PATH  = os.path.join(BASE_DIR, 'feedback.csv')
API_KEY_PATH   = os.path.join(BASE_DIR, 'api_key.txt')
QUERIES_BASIC_JSONL = os.path.join(BASE_DIR, 'queries_basic.jsonl')
QUERIES_FULL_JSONL = os.path.join(BASE_DIR, 'queries_full.jsonl')

# V10 sidecars
SYNONYMS_PATH       = os.path.join(BASE_DIR, 'melton_synonyms.json')
KNOWLEDGE_META_PATH = os.path.join(BASE_DIR, 'knowledge_meta.json')

# ── Startup tracking ──────────────────────────────────────────────────────────
SERVER_START_TIME = time.time()
TOTAL_QUERIES = 0

# ── API key ──────────────────────────────────────────────────────────────────
def get_api_key():
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if key:
        return key
    if os.path.exists(API_KEY_PATH):
        with open(API_KEY_PATH) as f:
            return f.read().strip()
    raise RuntimeError('No ANTHROPIC_API_KEY found in environment or api_key.txt')

# ── Knowledge base ───────────────────────────────────────────────────────────
def load_knowledge():
    if os.path.exists(KNOWLEDGE_PATH):
        with open(KNOWLEDGE_PATH, encoding='utf-8') as f:
            return f.read()
    return ''

KNOWLEDGE = load_knowledge()

# ═════════════════════════════════════════════════════════════════════════════
# ██ V10 LAYER — synonyms, normaliser, phonetic, resolver, pdf_lookup ████████
# ═════════════════════════════════════════════════════════════════════════════

def load_synonyms():
    if not os.path.exists(SYNONYMS_PATH):
        print(f'[V10] synonyms file not found at {SYNONYMS_PATH} — normaliser is a no-op')
        return {}
    try:
        with open(SYNONYMS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[V10][WARN] failed to load synonyms: {e}')
        return {}

def load_knowledge_meta():
    if not os.path.exists(KNOWLEDGE_META_PATH):
        print(f'[V10] knowledge_meta file not found at {KNOWLEDGE_META_PATH} — /pdf_lookup disabled')
        return {'pdfs': [], '_meta': {}}
    try:
        with open(KNOWLEDGE_META_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'[V10][WARN] failed to load knowledge_meta: {e}')
        return {'pdfs': [], '_meta': {}}

SYNONYMS       = load_synonyms()
KNOWLEDGE_META = load_knowledge_meta()

GLOBAL_SUBS           = SYNONYMS.get('global_substitutions', {}) or {}
CHILD_HINTS           = SYNONYMS.get('child_vocabulary_hints', {}) or {}
PHONETIC_CONFUSABLES  = SYNONYMS.get('phonetic_confusables', {}) or {}
CATEGORIES_V10        = SYNONYMS.get('categories', {}) or {}
FALLBACK_RULES        = SYNONYMS.get('fallback_rules', {}) or {}

_PDF_LIST = KNOWLEDGE_META.get('pdfs', []) or []

REDIRECT_PHRASES = {}
for cat_key, cat_body in CATEGORIES_V10.items():
    for phrase, target in (cat_body.get('redirect_phrases') or {}).items():
        REDIRECT_PHRASES[phrase.lower()] = target

SYNONYM_TO_CATEGORY = {}
for cat_key, cat_body in CATEGORIES_V10.items():
    bucket = (
        (cat_body.get('canonical')      or []) +
        (cat_body.get('lay_synonyms')   or []) +
        (cat_body.get('misspellings')   or []) +
        (cat_body.get('voice_garbles')  or []) +
        (cat_body.get('child_terms')    or []) +
        (cat_body.get('senior_terms')   or [])
    )
    for term in bucket:
        SYNONYM_TO_CATEGORY.setdefault(term.lower(), cat_body.get('canonical_category', cat_key))

# Metaphone (optional)
try:
    from metaphone import doublemetaphone
    _METAPHONE_OK = True
except Exception as _e:
    _METAPHONE_OK = False
    def doublemetaphone(s):
        return ('', '')
    print(f'[V10] metaphone not installed ({_e}); phonetic fallback disabled.')

_PHONETIC_SEEDS = {}
if _METAPHONE_OK:
    for cat_key, cat_body in CATEGORIES_V10.items():
        target_cat = cat_body.get('canonical_category', cat_key)
        for seed in (cat_body.get('phonetic_seeds') or []):
            p1, p2 = doublemetaphone(seed)
            if p1: _PHONETIC_SEEDS.setdefault(p1, []).append((seed, target_cat))
            if p2: _PHONETIC_SEEDS.setdefault(p2, []).append((seed, target_cat))

_WORD_RE = re.compile(r"[A-Za-z']+")

def normalise_query(q: str) -> str:
    if not q:
        return ''
    out = q.strip().lower()
    for phrase in sorted(CHILD_HINTS.keys(), key=len, reverse=True):
        if phrase in out:
            out = out.replace(phrase, CHILD_HINTS[phrase])
    def _sub(match):
        tok = match.group(0).lower()
        return GLOBAL_SUBS.get(tok, tok)
    out = _WORD_RE.sub(_sub, out)
    return re.sub(r'\s+', ' ', out).strip()

def phonetic_match(q: str):
    if not _METAPHONE_OK or not q:
        return ('', '')
    for token in _WORD_RE.findall(q.lower()):
        if len(token) < 3:
            continue
        p1, p2 = doublemetaphone(token)
        for p in (p1, p2):
            hits = _PHONETIC_SEEDS.get(p)
            if hits:
                seed, cat = hits[0]
                return (cat, seed)
    return ('', '')

_REWRITE_SYS = (
    "You rewrite resident queries about the City of Melton into clear canonical form. "
    "Return JSON ONLY with keys: {\"rewritten\":\"…\",\"category\":\"…\",\"confidence\":0.0-1.0}. "
    "Categories: waste_bins, rates_payments, rates_hardship, rates_concessions, planning_building, "
    "animals_pets, roads_traffic, parking, water_stormwater, environment_climate, emergency_bushfire, "
    "health_safety, families_children, aged_disability, community_events, library_learning, "
    "recreation_sport, governance_contact, business_economy, arts_culture_heritage, pdf_document_search, other."
)

def claude_rewrite(q: str, timeout: int = 10):
    try:
        api_key = get_api_key()
    except Exception:
        return None
    payload = json.dumps({
        'model': 'claude-sonnet-4-6',
        'max_tokens': 200,
        'system': _REWRITE_SYS,
        'messages': [{'role': 'user', 'content': f'Rewrite: {q}'}]
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        raw = data['content'][0]['text'].strip()
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:
        print(f'[V10][WARN] claude_rewrite failed: {e}')
        return None

def resolve_query(raw_query: str, allow_rewrite: bool = True) -> dict:
    nq = normalise_query(raw_query or '')
    for phrase, cat in REDIRECT_PHRASES.items():
        if phrase in nq:
            return {'category': cat, 'confidence': 1.0, 'normalised': nq,
                    'stage': 'redirect_phrase', 'matched': phrase}
    multi_hits = [t for t in SYNONYM_TO_CATEGORY.keys() if ' ' in t and t in nq]
    if multi_hits:
        best = max(multi_hits, key=len)
        return {'category': SYNONYM_TO_CATEGORY[best], 'confidence': 0.9,
                'normalised': nq, 'stage': 'synonym_multi', 'matched': best}
    tokens = set(_WORD_RE.findall(nq))
    single_hits = [t for t in tokens if t in SYNONYM_TO_CATEGORY]
    if single_hits:
        best = single_hits[0]
        return {'category': SYNONYM_TO_CATEGORY[best], 'confidence': 0.8,
                'normalised': nq, 'stage': 'synonym_single', 'matched': best}
    cat, seed = phonetic_match(nq)
    if cat:
        return {'category': cat, 'confidence': 0.6, 'normalised': nq,
                'stage': 'phonetic', 'matched': seed}
    if allow_rewrite:
        rw = claude_rewrite(raw_query)
        if rw and rw.get('category') and rw.get('category') != 'other':
            return {'category': rw['category'],
                    'confidence': float(rw.get('confidence', 0.55) or 0.55),
                    'normalised': nq, 'stage': 'claude_rewrite',
                    'rewritten': rw.get('rewritten', ''), 'matched': ''}
    return {'category': 'other', 'confidence': 0.0, 'normalised': nq,
            'stage': 'fallback', 'matched': '',
            'fallback_message': FALLBACK_RULES.get('if_no_match', '')}

def _score_pdf(q_tokens: set, pdf: dict) -> float:
    corpus = ' '.join([
        (pdf.get('filename') or '').replace('.pdf', '').replace('-', ' ').replace('_', ' '),
        (pdf.get('source_url') or ''),
    ]).lower()
    doc_tokens = set(_WORD_RE.findall(corpus))
    if not doc_tokens or not q_tokens:
        return 0.0
    overlap = len(q_tokens & doc_tokens)
    return overlap / max(len(q_tokens), 1)

def pdf_lookup(raw_query: str, top_k: int = 3) -> dict:
    if not _PDF_LIST:
        return {'status': 'unavailable', 'message': 'knowledge_meta.json not loaded'}
    nq = normalise_query(raw_query or '')
    q_tokens = set(_WORD_RE.findall(nq))

    exact = []
    for pdf in _PDF_LIST:
        fn_l = (pdf.get('filename') or '').lower()
        if nq and nq in fn_l:
            exact.append((1.0, pdf))
    if exact:
        exact.sort(key=lambda x: -len(x[1].get('filename') or ''))
        best = exact[0][1]
        return {'status': 'ok',
                'filename': best.get('filename'),
                'url': best.get('source_url'),
                'confidence': 0.95,
                'match_type': 'filename_substring'}

    candidates = []
    for pdf in _PDF_LIST:
        s = _score_pdf(q_tokens, pdf)
        if s > 0:
            candidates.append((s, pdf))
    candidates.sort(key=lambda x: -x[0])

    if not candidates:
        return {'status': 'no_match', 'confidence': 0.0,
                'fallback': '(03) 9747 7200', 'normalised': nq}
    best_score, best_pdf = candidates[0]
    if best_score >= 0.75:
        return {'status': 'ok',
                'filename': best_pdf.get('filename'),
                'url': best_pdf.get('source_url'),
                'confidence': round(best_score, 3),
                'match_type': 'token_overlap'}
    if best_score >= 0.40:
        top3 = [{'filename': p.get('filename'),
                 'url':      p.get('source_url'),
                 'score':    round(s, 3)}
                for s, p in candidates[:top_k]]
        return {'status': 'clarify', 'confidence': best_score,
                'question': "I have a few candidates — which one did you mean?",
                'top3': top3, 'normalised': nq}
    return {'status': 'no_match', 'confidence': best_score,
            'fallback': '(03) 9747 7200',
            'closest': {'filename': best_pdf.get('filename'),
                        'url':      best_pdf.get('source_url'),
                        'score':    round(best_score, 3)},
            'normalised': nq}

def suggest(raw_query: str, limit: int = 8):
    if not raw_query:
        return []
    nq = normalise_query(raw_query)
    prefix = nq
    hits = []
    for phrase in REDIRECT_PHRASES.keys():
        if phrase.startswith(prefix):
            hits.append((3.0, phrase))
    for term in SYNONYM_TO_CATEGORY.keys():
        if term.startswith(prefix) and ' ' not in term:
            hits.append((2.0, term))
    for phrase in REDIRECT_PHRASES.keys():
        if prefix in phrase and not phrase.startswith(prefix):
            hits.append((1.0, phrase))
    seen, out = set(), []
    for _, v in sorted(hits, key=lambda x: (-x[0], x[1])):
        if v in seen:
            continue
        seen.add(v); out.append(v)
        if len(out) >= limit:
            break
    return out

# ═════════════════════════════════════════════════════════════════════════════
# ██ End V10 LAYER ████████████████████████████████████████████████████████████
# ═════════════════════════════════════════════════════════════════════════════

def filter_pii(text: str) -> str:
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[EMAIL]', text)
    text = re.sub(r'\b\d{3,4}\s?\d{3,4}\b', '[PHONE]', text)
    text = re.sub(r'\b\d{4}\b', '[POSTCODE]', text)
    text = re.sub(r'\b\d{10}\b', '[ID_NUMBER]', text)
    return text

def log_query_basic(query: str, category: str):
    filtered_query = filter_pii(query)
    record = {
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'category': category,
        'query_preview': filtered_query[:200]
    }
    try:
        with open(QUERIES_BASIC_JSONL, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        print(f'[WARN] Failed to log basic query: {e}')

def log_query_full(query: str, response: str, category: str):
    filtered_query = filter_pii(query)
    filtered_response = filter_pii(response)
    record = {
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'category': category,
        'query': filtered_query,
        'response': filtered_response[:500]
    }
    try:
        with open(QUERIES_FULL_JSONL, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        print(f'[WARN] Failed to log full query: {e}')

# ── System prompt (Melton-specific) ──────────────────────────────────────────
SYSTEM_PROMPT = f"""You are Council Genius, the official AI resident assistant for the City of Melton, deployed by TechEntity on behalf of Melton City Council.

YOUR PURPOSE: Answer questions so completely the resident never needs to contact Council.

AGENTIC BEHAVIOUR: For any process question, give all steps, fees, correct forms, and correct officer.

FOR BUILDING/PLANNING: Ask property address (suburb), use of the building, and whether an overlay applies before answering.

FOR WASTE / BIN QUESTIONS: City of Melton uses a per-address waste lookup. Ask for the resident's address and direct them to [www.melton.vic.gov.au/My-Area](https://www.melton.vic.gov.au/My-Area) rather than asserting a fixed bin-day for an area. Remember Melton's schedule is: RED bin weekly, YELLOW recycling fortnightly, GREEN FOGO fortnightly (on the alternate week to yellow).

FOR RATES: City of Melton uses Capital Improved Value (CIV). Rates are paid in FOUR instalments: 30 Sep / 30 Nov / 28 Feb / 31 May. See knowledge §3.1–§3.3.

FOR PARKING: clarify whether the question is about a permit, a fine, or parking in a specific precinct before answering.

FOR PETS: remind the resident that dogs and cats 3+ months must be microchipped AND registered, and that all registered cats must be desexed (s.10A(1) Domestic Animals Act 1994). Council does NOT register pocket pets (s.33A DAA 1994).

FOUR-TURN RESOLUTION: Resolve every query within four user inputs.

OUT OF AREA: If a query is about a location or service clearly outside the City of Melton (e.g., a suburb that is in Wyndham, Brimbank, Moorabool, Hume, Greater Geelong, or Macedon Ranges), respond with only:
- One sentence noting it is outside the City of Melton area
- Full official name of the relevant council
- That authority's main phone number only
No links. No further elaboration. No offers of further help.

Neighbouring councils and numbers (use the council's FULL official name — never abbreviated):
- City of Wyndham (Werribee, Point Cook, Hoppers Crossing, Tarneit, Wyndham Vale, Truganina south of Melton boundary): [1300 023 411](tel:1300023411)
- Brimbank City Council (Sunshine, Deer Park, St Albans, Keilor, Burnside east of Melton boundary): [03 9249 4000](tel:0392494000)
- Moorabool Shire Council (Bacchus Marsh, Ballan, Parwan west of Melton boundary): [03 5366 7100](tel:0353667100)
- Hume City Council (Broadmeadows, Craigieburn, Sunbury, Diggers Rest north of Melton boundary): [03 9205 2200](tel:0392052200)
- City of Greater Geelong (Geelong, Lara, Ocean Grove, Torquay): [03 5272 5272](tel:0352725272)
- Macedon Ranges Shire Council (Gisborne, Woodend, Kyneton, Romsey): [03 5422 0333](tel:0354220333)
- Public Transport Victoria (tram, train, bus, myki questions): [1800 800 007](tel:1800800007)
- Department of Transport and Planning / VicRoads (state arterial roads, driver licensing): [13 11 70](tel:131170)

MULTILINGUAL: If asked in another language, answer in that language then repeat in English labelled "English version:"

COMMUNICATION STYLE — apply to every response:
- Use Australian English spelling and plain English. "Use" not "utilise." "About" not "regarding." "Help" not "facilitate." "Start" not "commence."
- Sentences average 15–20 words. One idea per sentence.
- Lead with what CAN be done before explaining any limitations.
- When a resident is reporting a problem that has already happened (a complaint), acknowledge their experience before providing process information. Do not jump straight to procedure.
- When a resident is asking for something to happen (a service request), respond efficiently with the information they need.
- Deliver bad news in this order: acknowledge, explain, offer next step.
- Never minimise a resident's concern. Never use "just," "only," "simply," or "it's easy."
- When you don't have specific information, say so clearly and direct the resident to the right contact — never invent fees, dates, or processes.

SERVICE REQUESTS vs COMPLAINTS:
- If the resident is asking for something to happen → answer efficiently.
- If the resident is expressing that something went wrong → acknowledge first, then inform.

FORMAT RULES — NON-NEGOTIABLE:
- NEVER use emoji of any kind
- NEVER output raw HTML — no <a> tags, no HTML elements of any kind whatsoever
- Phone numbers: markdown hyperlink ONLY — [(03) 9747 7200](tel:0397477200)
  Never bare digits. Never plain text alongside a link.
- Emails: markdown mailto ONLY — [csu@melton.vic.gov.au](mailto:csu@melton.vic.gov.au)
  Never plain text alongside a link.
- URLs: markdown links ONLY — [descriptive label](https://full-url)
  Never bare URLs. Never HTML anchor tags.
- Use **bold** for key terms
- Use bullet lists for multi-step processes
- Keep responses under 300 words unless a complex process genuinely requires more
- Do NOT use ## headers — use **bold text** instead
- Include the council contact footer no more than once per response

KNOWLEDGE BASE — CITY OF MELTON:

{KNOWLEDGE}

END OF KNOWLEDGE BASE.

If information is not in the knowledge base, direct the resident to [(03) 9747 7200](tel:0397477200) or [csu@melton.vic.gov.au](mailto:csu@melton.vic.gov.au). Do not invent fees, dates, or processes.
"""

# ── Analytics categories (Melton-tuned) ──────────────────────────────────────
CATEGORIES = {
    'rates':            ['rate', 'rates', 'levy', 'civ', 'capital improved value', 'municipal charge', 'payment plan', 'hardship', 'instalment', 'rebate', 'concession', 'valuation', 'rate cap', 'pensioner concession', 'differential rating', 'waste charge', 'flexipay'],
    'waste_bins':       ['bin', 'bins', 'rubbish', 'recycling', 'green waste', 'collection', 'fogo', 'kerbside', 'compost', 'organics', 'hard waste', 'hard rubbish', 'landfill', 'red bin', 'yellow bin', 'green bin', 'glass bin', 'missed bin', 'mrf', 'melton recycling facility'],
    'planning':         ['planning', 'planning permit', 'subdivision', 'zoning', 'overlay', 'heritage', 'development plan', 'rezoning', 'melton planning scheme', 'amendment', 'vicsmart', 'icp', 'infrastructure contributions', 'secondary dwelling'],
    'building':         ['building permit', 'building surveyor', 'building inspection', 'construction', 'demolition', 'owner builder', 'vba', 'compliance', 'pool', 'spa', 'barrier', 'esm', 'essential safety'],
    'parking':          ['parking', 'parking permit', 'residential parking permit', 'fine', 'infringement', 'appeal', 'parking zone', 'dispute fine', 'pinforce', 'clearway', 'accessible parking'],
    'animals':          ['dog', 'cat', 'animal', 'pet', 'register', 'pound', 'roaming', 'attack', 'barking', 'dangerous dog', 'microchip', 'desexed', 'off-leash', 'cat curfew', 'pocket pet'],
    'local_laws':       ['local law', 'noise', 'nuisance', 'skip bin', 'shipping container', 'footpath trading', 'outdoor dining', 'busking', 'nature strip', 'graffiti', 'open-air burning', 'fire prevention notice'],
    'roads':            ['road', 'footpath', 'pothole', 'kerb', 'drainage', 'street light', 'signage', 'driveway', 'vehicle crossing', 'street tree', 'road closure', 'bike lane', 'cycling', 'crossover', 'asset protection'],
    'transport':        ['train', 'bus', 'myki', 'ptv', 'public transport', 'vline', 'v/line', 'melton line', 'cobblebank station', 'rockbank station', 'sunshine station'],
    'utilities':        ['water', 'sewer', 'sewerage', 'electricity', 'power', 'outage', 'gas leak', 'powercor', 'greater western water', 'gww', 'melbourne water', 'nbn'],
    'venues_events':    ['venue', 'hire', 'event', 'permit', 'book', 'facility', 'library', 'melton library', 'learning hub', 'caroline springs library', 'waves', 'melton waves', 'leisure centre', 'djerriwarrh festival', 'carols', 'citizenship ceremony'],
    'community':        ['grant', 'program', 'service', 'aged', 'older', 'disability', 'youth', 'maternal', 'child health', 'mch', 'kindergarten', 'kinder', 'immunisation', 'multicultural', 'lgbtiq', 'volunteer', 'neighbourhood house'],
    'first_nations':    ['aboriginal', 'first nations', 'traditional owner', 'wadawurrung', 'bunurong', 'wurundjeri', 'woi wurrung', 'kulin', 'kulin nations', 'reconciliation action plan', 'rap', 'naidoc'],
    'governance':       ['meeting', 'councillor', 'mayor', 'deputy mayor', 'carli', 'zada', 'ceo', 'roslyn wai', 'agenda', 'minutes', 'foi', 'freedom of information', 'complaint', 'petition', 'council plan', 'ombudsman', 'governance', 'annual report', 'local government act', 'audit and risk'],
    'economy':          ['business', 'invest melton', 'innovation', 'startup', 'grant', 'tourism', 'eynesbury', 'cobblebank hub', 'logistics'],
    'environment':      ['climate', 'net zero', 'emissions reduction', 'biodiversity', 'urban forest', 'tree planting', 'pinkerton', 'eynesbury forest', 'toolern creek', 'melton reservoir', 'grassland'],
    'emergency':        ['emergency', 'flood', 'heatwave', 'bushfire', 'fire danger', 'relief centre', 'ses', 'cfa', 'family violence', '1800respect', 'safe steps', 'orange door', 'lifeline', 'beyond blue', 'recovery', 'disaster', 'bal', 'bushfire survival plan'],
    'off_topic_benign': ['recipe', 'football', 'weather', 'stock price', 'poem', 'news', 'sport', 'joke', 'movie', 'afl'],
    'potential_api_abuse': ['ignore previous', 'jailbreak', 'pretend you are', 'act as', 'system prompt', 'disregard', 'override', 'forget instructions', 'new instructions', 'ignore all'],
    'other':            []
}

def classify(text: str) -> str:
    lower = text.lower()
    for category, keywords in CATEGORIES.items():
        if category == 'other':
            continue
        if any(kw in lower for kw in keywords):
            return category
    return 'other'

def log_analytics(category: str, query: str):
    exists = os.path.exists(ANALYTICS_PATH)
    with open(ANALYTICS_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(['timestamp', 'category', 'query_preview'])
        w.writerow([
            datetime.datetime.utcnow().isoformat(),
            category,
            query[:120].replace('\n', ' ')
        ])

def log_feedback(query: str, response: str, rating: str):
    exists = os.path.exists(FEEDBACK_PATH)
    with open(FEEDBACK_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(['timestamp', 'rating', 'query_preview', 'response_preview'])
        w.writerow([
            datetime.datetime.utcnow().isoformat(),
            rating,
            query[:120].replace('\n', ' '),
            response[:200].replace('\n', ' ')
        ])

def call_claude(messages: list) -> str:
    api_key = get_api_key()
    payload = json.dumps({
        'model': 'claude-sonnet-4-6',
        'max_tokens': 1024,
        'system': SYSTEM_PROMPT,
        'messages': messages
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=payload,
        headers={
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return data['content'][0]['text']
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Anthropic API error {e.code}: {body}')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200):
        body = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: str, content_type: str):
        if not os.path.isfile(path):
            self._send_text('Not found', 404)
            return
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/health':
            knowledge_hash = hashlib.sha256(KNOWLEDGE.encode('utf-8')).hexdigest()[:16]
            knowledge_lines = len(KNOWLEDGE.split('\n'))
            uptime = time.time() - SERVER_START_TIME
            health = {
                'status': 'ok',
                'council': 'City of Melton',
                'knowledge_lines': knowledge_lines,
                'knowledge_hash': knowledge_hash,
                'prompt_version': '1.0',
                'uptime_seconds': round(uptime, 2),
                'total_queries': TOTAL_QUERIES,
                'model': 'claude-sonnet-4-6',
                'bin_mode': 'per-address-lookup',
                'server_version': 'v10.3',
                'synonyms_loaded': bool(SYNONYMS),
                'synonym_categories': len(CATEGORIES_V10),
                'knowledge_meta_loaded': bool(_PDF_LIST),
                'pdfs_indexed': len(_PDF_LIST),
                'phonetic_enabled': _METAPHONE_OK,
            }
            self._send_json(health)
            return

        if path == '/pdf_lookup':
            qs = parse_qs(parsed.query or '')
            q = (qs.get('q', [''])[0] or '').strip()
            if not q:
                self._send_json({'error': 'missing q parameter'}, 400)
                return
            try:
                result = pdf_lookup(q)
                log_analytics('pdf_lookup:' + result.get('status', '?'), q)
                self._send_json(result)
            except Exception as e:
                print(f'[ERROR /pdf_lookup] {e}')
                self._send_json({'error': str(e)}, 500)
            return

        if path == '/suggest':
            qs = parse_qs(parsed.query or '')
            q = (qs.get('q', [''])[0] or '').strip()
            if not q:
                self._send_json({'suggestions': []})
                return
            try:
                self._send_json({'suggestions': suggest(q)})
            except Exception as e:
                print(f'[ERROR /suggest] {e}')
                self._send_json({'error': str(e)}, 500)
            return

        if path == '/' or path == '/index.html':
            self._serve_file(os.path.join(BASE_DIR, 'page.html'), 'text/html; charset=utf-8')
            return

        if path == '/page.html':
            self._serve_file(os.path.join(BASE_DIR, 'page.html'), 'text/html; charset=utf-8')
            return

        if path.startswith('/pdfs/'):
            safe = os.path.normpath(path.lstrip('/'))
            full = os.path.join(BASE_DIR, safe)
            pdfs_dir = os.path.join(BASE_DIR, 'pdfs')
            if not full.startswith(pdfs_dir):
                self._send_text('Forbidden', 403)
                return
            self._serve_file(full, 'application/pdf')
            return

        if path.startswith('/images/'):
            safe = os.path.normpath(path.lstrip('/'))
            full = os.path.join(BASE_DIR, safe)
            images_dir = os.path.join(BASE_DIR, 'images')
            if not full.startswith(images_dir):
                self._send_text('Forbidden', 403)
                return
            ext = os.path.splitext(safe)[1].lower()
            ct = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.svg': 'image/svg+xml',
                '.gif': 'image/gif',
                '.ico': 'image/x-icon',
                '.webp': 'image/webp'
            }.get(ext, 'application/octet-stream')
            self._serve_file(full, ct)
            return

        if path == '/admin/analytics':
            if os.path.exists(ANALYTICS_PATH):
                with open(ANALYTICS_PATH, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.send_header('Content-Disposition', 'attachment; filename="analytics.csv"')
                self.send_header('Content-Length', str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_text('category,query_preview,timestamp\n', 200)
            return

        if path == '/admin/feedback':
            if os.path.exists(FEEDBACK_PATH):
                with open(FEEDBACK_PATH, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.send_header('Content-Disposition', 'attachment; filename="feedback.csv"')
                self.send_header('Content-Length', str(len(data)))
                self._cors()
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_text('timestamp,rating,query_preview,response_preview\n', 200)
            return

        self._send_text('Not found', 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get('Content-Length', 0))
        body_bytes = self.rfile.read(length) if length else b''

        if path == '/chat':
            try:
                body = json.loads(body_bytes.decode('utf-8'))
                messages = body.get('messages', [])
                if not messages:
                    self._send_json({'error': 'No messages provided'}, 400)
                    return

                last_user = next(
                    (m['content'] for m in reversed(messages) if m['role'] == 'user'),
                    ''
                )

                v10 = resolve_query(last_user, allow_rewrite=False)
                v9_category = classify(last_user)
                category = v10['category'] if v10['category'] != 'other' else v9_category

                log_analytics(category, last_user)
                log_query_basic(last_user, category)

                if v9_category == 'potential_api_abuse' or category == 'potential_api_abuse':
                    self._send_json({'error': 'This request cannot be processed.'}, 400)
                    return

                if category == 'other':
                    rw = claude_rewrite(last_user)
                    if rw and rw.get('category') and rw['category'] != 'other':
                        category = rw['category']

                global TOTAL_QUERIES
                TOTAL_QUERIES += 1
                reply = call_claude(messages)
                log_query_full(last_user, reply, category)
                self._send_json({
                    'reply': reply,
                    'category': category,
                    'v10': {
                        'stage': v10.get('stage'),
                        'confidence': v10.get('confidence'),
                        'normalised': v10.get('normalised'),
                    },
                })

            except Exception as e:
                print(f'[ERROR /chat] {e}')
                error_msg = 'Sorry, I\'m having trouble right now. Please try again or call City of Melton on (03) 9747 7200.'
                self._send_json({'error': error_msg}, 500)
            return

        if path == '/feedback':
            try:
                body = json.loads(body_bytes.decode('utf-8'))
                log_feedback(
                    body.get('query', ''),
                    body.get('response', ''),
                    body.get('rating', 'unknown')
                )
                self._send_json({'ok': True})
            except Exception as e:
                print(f'[ERROR /feedback] {e}')
                self._send_json({'error': str(e)}, 500)
            return

        self._send_text('Not found', 404)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'Council Genius — City of Melton — listening on port {port}')
    server.serve_forever()
