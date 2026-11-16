"""eBay Browse API integration; avoids scraping eBay listing HTML."""
from __future__ import annotations

import html
from decimal import Decimal

import requests
from bs4 import BeautifulSoup

from core import ebay_item_id, money

API_ROOT = 'https://api.ebay.com'


class EbayError(RuntimeError):
    pass


def _response_error(response):
    try:
        data = response.json()
        return '; '.join(e.get('message', '') for e in data.get('errors', [])) or response.text[:250]
    except ValueError:
        return response.text[:250]


def fetch_access_token(client_id, client_secret):
    response = requests.post(API_ROOT + '/identity/v1/oauth2/token',
                             auth=(client_id, client_secret),
                             data={'grant_type': 'client_credentials', 'scope': 'https://api.ebay.com/oauth/api_scope'},
                             timeout=18)
    if not response.ok:
        raise EbayError(f'eBay authentication failed ({response.status_code}): {_response_error(response)}')
    return response.json()['access_token']


def _pounds(entry):
    try:
        if entry.get('currency') != 'GBP':
            return None
        return money(entry['value'])
    except (AttributeError, TypeError, KeyError):
        return None


def _extract_pictures(data):
    images = []
    for key in ('image', 'additionalImages', 'thumbnailImages'):
        x = data.get(key, [])
        for item in x if isinstance(x, list) else [x]:
            if isinstance(item, dict):
                url = item.get('imageUrl', '')
                if url.startswith('https://') and url not in images:
                    images.append(url)
    return images[:10]


def plain_text(html_content):
    soup = BeautifulSoup(html.unescape(str(html_content or '')), 'html.parser')
    for tag in soup(['style', 'script', 'noscript']):
        tag.decompose()
    return ' '.join(soup.stripped_strings)[:16000]


def parse_listing_api(data, source_url, legacy_id):
    bid = _pounds(data.get('currentBidPrice'))
    fixed = _pounds(data.get('price'))
    options = data.get('buyingOptions', [])
    auction = 'AUCTION' in options and ('FIXED_PRICE' not in options or bid is not None)
    price = bid if auction and bid is not None else fixed or bid
    postage = []
    for opt in data.get('shippingOptions', []):
        v = _pounds(opt.get('shippingCost'))
        if v is not None:
            postage.append(v)
    return dict(id=legacy_id, url=source_url, title=data.get('title', ''),
                description=plain_text(data.get('description', '') or data.get('shortDescription', '')),
                condition=data.get('condition', ''),
                price_gbp=float(price) if price is not None else None,
                price_kind='Current bid' if auction and bid is not None else 'Buy it now / asking price',
                shipping_gbp=float(min(postage)) if postage else 0.,
                shipping_known=bool(postage),
                image_urls=_extract_pictures(data), models=[], revision=0,
                error='', is_demo=False)


def fetch_listing(url, token):
    item_id = ebay_item_id(url)
    resp = requests.get(API_ROOT + '/buy/browse/v1/item/get_item_by_legacy_id',
                        params={'legacy_item_id': item_id},
                        headers={'Authorization': f'Bearer {token}', 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_GB',
                                 'Accept': 'application/json'}, timeout=18)
    if not resp.ok:
        raise EbayError(f'eBay could not retrieve item {item_id} ({resp.status_code}): {_response_error(resp)}')
    return parse_listing_api(resp.json(), url, item_id)


def manual_listing(url, error=''):
    item_id = ebay_item_id(url)
    return dict(id=item_id, url=url, title=f'eBay item {item_id}', description='', condition='',
                price_gbp=None, price_kind='Entered manually', shipping_gbp=0., shipping_known=False,
                image_urls=[], models=[], revision=0, error=error, is_demo=False)
