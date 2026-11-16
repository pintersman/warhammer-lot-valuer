"""Pure validation, matching, and valuation logic for Warhammer Lot Valuer."""
from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qs, quote, urlparse

from rapidfuzz import fuzz, process

BUY_RATIO = Decimal('0.49')
CENT = Decimal('0.01')
CATALOGUE_COLUMNS = ['product_name', 'price_gbp', 'models_per_box', 'product_url', 'checked_on']
MODEL_COLUMNS = ['model_name', 'quantity', 'basis', 'confidence', 'notes']


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value):
    return f'£{money(value):,.2f}'


def ebay_item_id(link: str) -> str:
    """Accept only regular eBay.com / eBay.co.uk item URLs; no network redirects."""
    u = urlparse((link or '').strip())
    host = (u.hostname or '').lower()
    if u.scheme not in ('http', 'https') or host not in (
        'ebay.co.uk', 'www.ebay.co.uk', 'm.ebay.co.uk',
        'ebay.com', 'www.ebay.com', 'm.ebay.com',
    ):
        raise ValueError('Use a full ebay.co.uk or ebay.com item URL, not a shortened link.')
    parts = [p for p in u.path.split('/') if p]
    if 'itm' in parts:
        for part in reversed(parts[parts.index('itm') + 1:]):
            if re.fullmatch(r'\d{9,15}', part):
                return part
    qs = parse_qs(u.query)
    for key in ('item', 'itemid', 'itemId'):
        for v in qs.get(key, []):
            if re.fullmatch(r'\d{9,15}', v):
                return v
    raise ValueError('No eBay item number found in this URL.')


def _d(value):
    if isinstance(value, Decimal):
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError('Missing number')
    if isinstance(value, bool):
        raise ValueError('Not a number')
    text = re.sub(r'[£,\s]', '', str(value))
    number = Decimal(text)
    if not number.is_finite():
        raise ValueError('Non-finite number')
    return number


def normalise_name(name):
    x = (name or '').lower()
    x = x.replace('&', ' and ')
    x = re.sub(r'\b(games workshop|warhammer 40,?000|warhammer 40k|age of sigmar|citadel miniatures)\b', ' ', x)
    x = re.sub(r'[^a-z0-9]+', ' ', x)
    return re.sub(r'\s+', ' ', x).strip()


def safe_product_url(url):
    url = (url or '').strip()
    if not url:
        return ''
    u = urlparse(url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError('Product URL must be an https:// or http:// link')
    return url


def clean_catalogue(records):
    """Validate user-authorised Wayland price records, do not fetch sites."""
    clean = []
    warnings = []
    dedup = {}
    for n, r in enumerate(records, 2):
        name = str(r.get('product_name', '') or '').strip()
        if not name:
            continue
        try:
            price = _d(r.get('price_gbp'))
            pack = _d(r.get('models_per_box', 1))
            if price <= 0 or pack <= 0 or pack != pack.to_integral_value():
                raise ValueError('price and models_per_box must be positive; pack count must be an integer')
            url = safe_product_url(r.get('product_url') or '')
            check = str(r.get('checked_on') or '').strip()
            if check and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', check):
                raise ValueError('checked_on must be YYYY-MM-DD')
            entry = dict(product_name=name, price_gbp=str(money(price)), models_per_box=int(pack), product_url=url, checked_on=check)
            dedup[normalise_name(name)] = entry
        except (ValueError, InvalidOperation, TypeError) as e:
            warnings.append(f'Row {n} ({name}): {e}')
    clean = list(dedup.values())
    return clean, warnings


def parse_catalogue_csv(content: bytes):
    try:
        text = content.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
        fields = set(reader.fieldnames or [])
        required = {'product_name', 'price_gbp', 'models_per_box'}
        if not required <= fields:
            raise ValueError('CSV requires product_name, price_gbp, models_per_box columns.')
        return clean_catalogue(list(reader))
    except UnicodeDecodeError as ex:
        raise ValueError('Please upload a UTF-8 CSV file.') from ex


def catalogue_csv(catalogue):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CATALOGUE_COLUMNS, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(catalogue)
    return out.getvalue().encode('utf-8-sig')


def sanitise_models(rows):
    result = []
    for r in rows:
        name = str(r.get('model_name') or '').strip()
        if not name:
            continue
        try:
            qty = _d(r.get('quantity'))
            if qty <= 0 or qty != qty.to_integral_value() or qty > 10000:
                continue
        except (ValueError, InvalidOperation, TypeError):
            continue
        basis = str(r.get('basis') or 'models').lower().strip()
        if basis not in ('models', 'boxes'):
            basis = 'models'
        confidence = str(r.get('confidence') or 'manual').lower().strip()
        if confidence not in ('high', 'medium', 'low', 'manual'):
            confidence = 'manual'
        result.append(dict(model_name=name, quantity=int(qty), basis=basis, confidence=confidence,
                           notes=str(r.get('notes') or '').strip()))
    return result


def match_catalogue(name, catalogue):
    """Only exact or unusually strong *unambiguous* matches are automatic."""
    if not catalogue:
        return {'auto': None, 'suggestion': None, 'score': 0, 'alternatives': []}
    norm = normalise_name(name)
    if not norm:
        return {'auto': None, 'suggestion': None, 'score': 0, 'alternatives': []}
    norms = [normalise_name(x['product_name']) for x in catalogue]
    matches = process.extract(norm, norms, scorer=fuzz.WRatio, limit=6)
    indices = [m[2] for m in matches]
    top = matches[0]
    margin = top[1] - matches[1][1] if len(matches) > 1 else 100
    auto = top[2] if top[1] >= 94 and margin >= 8 else None
    if top[1] == 100 and norms.count(norm) == 1:
        auto = top[2]
    return dict(auto=auto, suggestion=top[2], score=round(top[1]), alternatives=indices)


def valuation(listing, catalogue, selected):
    """selected: index per model line, or None. Prices are kit-equivalent fractions."""
    detail = []
    known = Decimal('0')
    for k, item in enumerate(listing.get('models', [])):
        index = selected[k] if k < len(selected) else None
        product = catalogue[index] if isinstance(index, int) and 0 <= index < len(catalogue) else None
        value = None
        if product:
            factor = Decimal(1) if item.get('basis') == 'boxes' else _d(product['models_per_box'])
            value = money(_d(product['price_gbp']) * _d(item['quantity']) / factor)
            known += value
        detail.append(dict(model_name=item['model_name'], quantity=item['quantity'], basis=item.get('basis', 'models'),
                           confidence=item.get('confidence', ''), notes=item.get('notes', ''),
                           matched_product=product['product_name'] if product else '',
                           wayland_kit_price=product['price_gbp'] if product else '',
                           models_per_box=product['models_per_box'] if product else '',
                           product_url=product['product_url'] if product else '',
                           line_wayland_value_gbp=value if product else None))
    complete = bool(detail) and all(d['line_wayland_value_gbp'] is not None for d in detail)
    total = money(known) if complete else None
    ceiling = money(total * BUY_RATIO) if complete else None
    listing_price = money(listing['price_gbp']) if listing.get('price_gbp') is not None and _d(listing['price_gbp']) > 0 else None
    shipping_known = bool(listing.get('shipping_known', False))
    ebay_cost = money(listing_price + _d(listing.get('shipping_gbp', 0))) if listing_price is not None and shipping_known else None
    difference = money(ceiling - ebay_cost) if ceiling is not None and ebay_cost is not None else None
    status = 'Under ceiling' if difference is not None and difference > 0 else 'At ceiling' if difference == 0 else 'Over ceiling' if difference is not None else 'Incomplete'
    return dict(lines=detail, matched_subtotal=money(known), complete=complete,
                wayland_total=total, ceiling=ceiling, ebay_cost=ebay_cost,
                difference=difference, status=status,
                remaining=sum(d['line_wayland_value_gbp'] is None for d in detail))


def retailer_search_url(model):
    """Search-engine navigation only; the app does not scrape Wayland pages."""
    return 'https://www.google.com/search?q=' + quote('site:waylandgames.co.uk ' + model)
