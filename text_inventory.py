"""Conservative, offline Warhammer inventory extraction from pasted seller text.

No API key is needed. The parser recognises common seller list formats and
leaves genuinely ambiguous prose for optional AI review. It never guesses a
number when one was not stated.
"""
from __future__ import annotations

import html
import re
from collections import OrderedDict

from bs4 import BeautifulSoup

from core import match_catalogue, normalise_name, sanitise_models

NUMBER_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20,
}
NUMBER = r'(?:\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)'
SQUAD = re.compile(
    rf'^\s*(?P<count>{NUMBER})\s+(?:squads?|units?|sets?)\s+of\s+'
    rf'(?P<each>{NUMBER})\s*(?:x\s*)?(?P<name>.+)$', re.I)
BOXES = re.compile(
    rf'^\s*(?P<count>{NUMBER})\s*(?:x\s*)?(?:sealed\s+|unopened\s+|new\s+)?'
    rf'(?:boxes|box|kits|kit|packs|pack)\s+of\s+(?P<name>.+)$', re.I)
FRONT = re.compile(rf'^\s*(?:x\s*)?(?P<count>{NUMBER})\s*(?:x|×|\*|pcs?\.?|models?|miniatures?)?\s+(?P<name>.+)$', re.I)
# Also handle compact "5x Intercessors" and "x5 Intercessors".
FRONT_COMPACT = re.compile(rf'^\s*(?:x\s*)?(?P<count>{NUMBER})\s*[x×]\s*(?P<name>.+)$', re.I)
BACK = re.compile(rf'^\s*(?P<name>.+?)\s+(?:x|×|qty\.?|quantity\s*:)\s*(?P<count>{NUMBER})\s*$', re.I)
BACK_PARENS = re.compile(rf'^\s*(?P<name>.+?)\s*\(\s*(?P<count>{NUMBER})\s*\)\s*$', re.I)
BACK_DASH = re.compile(rf'^\s*(?P<name>.+?)\s*[-–:]\s*(?P<count>{NUMBER})\s*$', re.I)
INTRO = re.compile(r'^(?:for sale\s*[:\-]?\s*|included\s*[:\-]?\s*|includes?\s*[:\-]?\s*|contains?\s*[:\-]?\s*|there (?:are|is)\s+|i (?:have|am selling)\s+|selling\s*[:\-]?\s*|lot (?:includes|contains)\s*[:\-]?\s*)+', re.I)
EXCLUDED = re.compile(r'^(?:not\s+included|excluded?|not\s+part\s+of\s+the\s+(?:sale|lot)|not\s+for\s+sale|without|does\s+not\s+include|no\s+models?\s+included)\b', re.I)
NOISE = re.compile(r'^(?:photos?|pictures?|images?|days?|weeks?|hours?|pounds?|gbp|£|postage|delivery|shipping|paint|paints|bases?|dice|books?|instructions?|sprues?|transfers?|stickers?|codex|rulebooks?|feedback|years?|items?|bits?|parts?|listings?|lots?|models?\b|miniatures?\b|sellers?|buyers?|payments?)\b', re.I)
CONDITION_END = re.compile(
    r'\s+(?:(?:that\s+are|which\s+are|all\s+are)\s+)?(?:painted|primed|assembled|built|unpainted|new\s+on\s+sprue|nos|on\s+sprue|unbuilt|glued|magnetised|magnetized|with\s+bases?|without\s+bases?|in\s+(?:great|good|used|excellent)\s+condition|in\s+the\s+photos?|as\s+pictured|as\s+shown|included\s+in\s+the\s+sale)\b.*$', re.I)
DUPLICATE_HINT = re.compile(r'\b(?:see\s+(?:the\s+)?(?:photos?|pictures?)|as\s+pictured|as\s+shown|mixed\s+lot|assorted|unknown\s+models?|unsure|approximately|roughly|about\s+\d+)\b', re.I)


def _number(text: str) -> int:
    return NUMBER_WORDS.get(text.casefold(), int(text) if text.isdigit() else 0)


def _segments(text: str) -> list[str]:
    # Preserve line breaks in user-pasted HTML, don't assume sellers use bullets.
    soup = BeautifulSoup(html.unescape(str(text or '')), 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    value = soup.get_text('\n', strip=True)
    value = value.replace('\r', '\n').replace('•', '\n').replace('·', '\n')
    value = re.sub(r'(?m)^\s*[-*]\s+(?=\S)', '', value)
    value = re.sub(r'\s*[;|]\s*', '\n', value)
    # Commas often divide models, but 40,000 is a game title, not a separator.
    value = re.sub(r'(?<!\d),\s*(?=(?:\d{1,4}|x\s*\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:x\b|\w))', '\n', value, flags=re.I)
    value = re.sub(r'\s+\band\b\s+(?=(?:x\s*)?(?:\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)(?:\s*[x×])?(?=\s|$))', '\n', value, flags=re.I)
    value = re.sub(r'\s*\+\s*(?=(?:x\s*)?\d)', '\n', value)
    return [line.strip() for line in value.split('\n') if line.strip()]


def _clean_name(name: str) -> str:
    name = str(name or '').strip(' .,:;–-\t\"\'')
    name = re.sub(r'^(?:of\s+|warhammer\s+40\s*,?\s*000\s+|40k\s+|games\s+workshop\s+|gw\s+)', '', name, flags=re.I)
    name = re.sub(r'^\d+\s*(?:models?|miniatures?)\s+(?:of\s+)?', '', name, flags=re.I)
    name = re.sub(r',?\s+(?:no\s+bases?\s+included|without\s+bases?|bases?\s+not\s+included).*$', '', name, flags=re.I)
    name = CONDITION_END.sub('', name)
    name = re.sub(r'\s+\((?:painted|unpainted|primed|built|assembled|nos|new|used|unbuilt)\)\s*$', '', name, flags=re.I)
    return name.strip(' .,:;–-\t\"\'')


def _extract_segment(segment: str):
    if EXCLUDED.match(segment.strip()):
        return None
    text = INTRO.sub('', segment.strip())
    text = re.sub(r'^(?:these|they)\s+are\s+', '', text, flags=re.I)
    text = re.sub(r'^warhammer\s+40\s*,?\s*000\s+', '', text, flags=re.I)
    text = re.sub(r'^.{1,80}:\s*(?=(?:x\s*)?(?:\d+|one|two|three|four|five)\b)', '', text, flags=re.I)
    text = re.sub(r'^\s*(?:[-*]\s*|\d+[.)]\s+(?=[x×\d]))', '', text)
    if EXCLUDED.match(text):
        return None
    basis = 'models'
    confidence = 'medium'
    squad = SQUAD.match(text)
    boxed = BOXES.match(text)
    if squad:
        qty = _number(squad['count']) * _number(squad['each'])
        name = squad['name']
    elif boxed:
        qty = _number(boxed['count'])
        name = boxed['name']
        name = re.sub(r'^\d+\s*(?:x\s*)?(?=\S)', '', name)
        basis = 'boxes'
    else:
        parsed = (FRONT_COMPACT.match(text) or FRONT.match(text)
                  or BACK.match(text) or BACK_PARENS.match(text) or BACK_DASH.match(text))
        if not parsed:
            return None
        qty = _number(parsed['count'])
        name = parsed['name']
    name = _clean_name(name)
    if (qty < 1 or qty > 10000 or not name or len(name) > 90 or not re.search('[A-Za-z]', name)
            or NOISE.match(name) or name.lower().startswith(('£', 'about ', 'approximately '))):
        return None
    if re.search(r'(?:\bnot\s+included\b|\bnot\s+part\s+of\s+the\s+sale\b)', name, re.I):
        # This text may qualify bases, but don't assert what's included.
        return None
    if re.search(r'\b(?:and|plus)\s+(?:\d+|one|two|three|four|five)\b', name, re.I):
        return None
    return dict(model_name=name, quantity=qty, basis=basis,
                confidence=confidence, notes='Automatically extracted from seller text; verify against listing photos')


def extract_text_inventory(description: str, title: str = '', catalogue=None):
    """Return (model lines, warnings); be conservative when the text is vague.

    Handles "5 Intercessors", "2x Rhino", "Overlord x 1", "1 box of
    Necron Warriors", "2 squads of 5 Intercessors", and similar formats.
    """
    original = (description or '').strip() or (title or '').strip()
    if not original:
        return [], ['No seller description is available to analyse.']
    warnings = []
    if DUPLICATE_HINT.search(original):
        warnings.append('Description refers to photos or uncertain quantities; check images for missing or duplicated models.')
    items = OrderedDict()
    catalogue = catalogue or []
    for segment in _segments(original[:16000]):
        found = _extract_segment(segment)
        if not found:
            continue
        if catalogue:
            match = match_catalogue(found['model_name'], catalogue)
            if match['auto'] is not None:
                found['model_name'] = catalogue[match['auto']]['product_name']
                found['confidence'] = 'high' if match['score'] == 100 else 'medium'
        key = (normalise_name(found['model_name']), found['basis'])
        if key in items:
            items[key]['quantity'] += found['quantity']
            items[key]['confidence'] = 'medium'
            items[key]['notes'] = 'Name appears more than once in the description; verify total count'
        else:
            items[key] = found
    rows = sanitise_models(items.values())
    if not rows and description and title and description.strip() != title.strip():
        rows, _ = extract_text_inventory(title, '', catalogue)
        if rows:
            warnings.append('No quantities were found in the description; counts were taken from the listing title. Verify these against photos.')
    if not rows:
        warnings.append('No model quantities could be identified automatically. Use AI identification for prose or photos, or check the description.')
    return rows, warnings
