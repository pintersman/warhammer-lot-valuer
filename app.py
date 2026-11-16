"""Streamlit UI for Warhammer Lot Valuer."""
from __future__ import annotations

import csv
import hashlib
import io
import os
from datetime import date

import pandas as pd
import streamlit as st

from core import (BUY_RATIO, CATALOGUE_COLUMNS, MODEL_COLUMNS, catalogue_csv,
                  clean_catalogue, ebay_item_id, match_catalogue, money,
                  money_str, parse_catalogue_csv, retailer_search_url,
                  sanitise_models, valuation)
from ebay_client import EbayError, fetch_access_token, fetch_listing, manual_listing
from vision import identify_models

st.set_page_config(page_title='Warhammer Lot Valuer', page_icon='⚔️', layout='wide')
st.markdown('''<style>
.block-container { max-width: 1260px; padding-top: 2.2rem; }
.hero-small { color: #AAB7B1; font-size: 0.9rem; letter-spacing: .13em; text-transform: uppercase; }
.hero-title { font-size: 2.6rem; font-weight: 780; letter-spacing: -.04em; margin: .1rem 0; }
.muted { color: #B3BDB9; }
[data-testid="stMetricValue"] { font-size: 1.8rem; }
</style>''', unsafe_allow_html=True)


if 'listings' not in st.session_state:
    st.session_state.listings = {}
if 'catalogue' not in st.session_state:
    st.session_state.catalogue = []
if 'catalogue_rev' not in st.session_state:
    st.session_state.catalogue_rev = 0
if 'match_overrides' not in st.session_state:
    st.session_state.match_overrides = {}
if 'ai_warnings' not in st.session_state:
    st.session_state.ai_warnings = {}


def secret_or_env(name):
    try:
        return str(st.secrets.get(name, '') or os.getenv(name, ''))
    except Exception:
        return os.getenv(name, '')


def render_csv(rows):
    if not rows:
        return b''
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()), extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: '' if v is None else v for k, v in row.items()})
    return out.getvalue().encode('utf-8-sig')


def demo():
    if 'demo-example' in st.session_state.listings:
        return
    if not st.session_state.catalogue:
        st.session_state.catalogue = [
            dict(product_name='Intercessors', price_gbp='40.00', models_per_box=10, product_url='', checked_on=''),
            dict(product_name='Primaris Captain', price_gbp='25.00', models_per_box=1, product_url='', checked_on=''),
        ]
        st.session_state.catalogue_rev += 1
    st.session_state.listings['demo-example'] = dict(
        id='demo-example', url='', title='DEMO: 5 Intercessors and 1 Primaris Captain',
        description='Illustrative example with invented prices, NOT a live eBay listing.',
        condition='Used', price_gbp=30., price_kind='Illustrative asking price',
        shipping_gbp=3., shipping_known=True, image_urls=[], revision=0,
        models=[dict(model_name='Intercessors', quantity=5, basis='models', confidence='manual', notes='Five loose infantry models'),
                dict(model_name='Primaris Captain', quantity=1, basis='models', confidence='manual', notes='One character')],
        error='', is_demo=True)


with st.sidebar:
    st.header('Connections')
    st.caption('Credentials stay in this app session or the private Streamlit secrets you configure.')
    ebay_id = st.text_input('eBay Client ID', value=secret_or_env('EBAY_CLIENT_ID'), type='password',
                            help='Get production eBay developer application credentials.')
    ebay_secret = st.text_input('eBay Client Secret', value=secret_or_env('EBAY_CLIENT_SECRET'), type='password')
    ai_key = st.text_input('OpenAI API key (optional)', value=secret_or_env('OPENAI_API_KEY'), type='password',
                           help='Used only when you click Identify models with AI. API usage is billed separately.')
    st.divider()
    st.markdown('**Pricing rule**')
    st.markdown('49% × Wayland equivalent retail total')
    st.caption('The 49% threshold is fixed. Shipping is included in the eBay cost when known.')
    st.divider()
    if st.button('Load illustrative demo', use_container_width=True):
        demo()
        st.rerun()
    st.caption('Demo figures are fictional, not actual retailer quotes.')

st.markdown('<div class="hero-small">Resale research workspace</div>', unsafe_allow_html=True)
st.markdown('<div class="hero-title">Warhammer Lot Valuer</div>', unsafe_allow_html=True)
st.caption('eBay listings  →  miniature inventory  →  Wayland reference prices  →  49% purchase ceiling')

tab_list, tab_prices, tab_results = st.tabs(['01  ·  eBay listings', '02  ·  Wayland price catalogue', '03  ·  Valuations'])

with tab_list:
    st.subheader('Import eBay listings')
    st.write('Paste one full eBay listing URL per line. Live fetching uses the official eBay Browse API; if credentials are absent or retrieval fails, you can enter the details manually.')
    links = st.text_area('eBay links', placeholder='https://www.ebay.co.uk/itm/123456789012\nhttps://www.ebay.co.uk/itm/987654321098',
                         height=116, key='listing_url_input')
    one, two, _ = st.columns([1.3, 1.0, 4])
    with one:
        add_pressed = st.button('Load eBay links', type='primary', use_container_width=True)
    with two:
        if st.button('Clear all listings', use_container_width=True):
            st.session_state.listings = {}
            st.session_state.match_overrides = {}
            st.rerun()
    if add_pressed:
        raw_links = [s.strip() for s in links.splitlines() if s.strip()][:30]
        if not raw_links:
            st.warning('Paste at least one eBay listing URL.')
        else:
            token = None
            if ebay_id and ebay_secret:
                try:
                    with st.spinner('Connecting to the eBay API…'):
                        token = fetch_access_token(ebay_id, ebay_secret)
                except Exception as exc:
                    st.warning('eBay API authentication could not be completed. Listings will be added for manual editing. ' + str(exc)[:250])
            added, failures = 0, []
            with st.spinner('Loading listings…'):
                for link in raw_links:
                    try:
                        item_id = ebay_item_id(link)
                        if item_id in st.session_state.listings:
                            continue
                        if token:
                            try:
                                listing = fetch_listing(link, token)
                            except (EbayError, ValueError) as exc:
                                listing = manual_listing(link, error=str(exc))
                        else:
                            listing = manual_listing(link, error='eBay API credentials not connected; please provide listing details manually.')
                        st.session_state.listings[item_id] = listing
                        added += 1
                    except ValueError as exc:
                        failures.append(f'{link[:90]} — {exc}')
            if added:
                st.success(f'Added {added} listing(s). Check the details and identify included models below.')
            for warning in failures:
                st.warning(warning)
    if not st.session_state.listings:
        st.info('Start by adding links, or use the illustrative demo in the sidebar to explore the workflow.')

    for listing_id, listing in list(st.session_state.listings.items()):
        title = listing['title'] or listing_id
        with st.expander(f"{title[:105]}  ·  {listing_id}", expanded=len(st.session_state.listings) == 1):
            if listing.get('is_demo'):
                st.warning('DEMO ONLY: these are invented values and are not evidence of actual eBay or Wayland prices.')
            if listing.get('error'):
                st.warning(listing['error'])
            if listing['url']:
                st.markdown(f'[Open eBay listing]({listing["url"]})')
            if st.button('Remove listing', key=f'remove_{listing_id}'):
                del st.session_state.listings[listing_id]
                st.rerun()
            listing['title'] = st.text_input('Title', value=listing['title'], key=f'title_{listing_id}')
            p1, p2, p3 = st.columns([1.3, 1, 1])
            with p1:
                price = st.number_input('Current eBay price (£)', min_value=0.0,
                                         value=float(listing['price_gbp'] or 0), step=1.0, format='%.2f',
                                         key=f'price_{listing_id}')
                listing['price_gbp'] = float(price) if price > 0 else None
                st.caption(listing.get('price_kind', 'Asking price'))
            with p2:
                listing['shipping_gbp'] = st.number_input('Postage (£)', min_value=0.,
                    value=float(listing.get('shipping_gbp', 0)), step=0.5, format='%.2f', key=f'shipping_{listing_id}')
            with p3:
                listing['shipping_known'] = st.checkbox('Postage verified (including £0)',
                    value=listing.get('shipping_known', False), key=f'shipping_known_{listing_id}',
                    help='Untick if postage depends on address or has not been checked.')
            listing['description'] = st.text_area('Seller description / included models',
                value=listing.get('description', ''), height=120, key=f'desc_{listing_id}')
            if listing.get('image_urls'):
                st.caption('Images returned by eBay')
                cols = st.columns(5)
                for k, url in enumerate(listing['image_urls'][:5]):
                    with cols[k]:
                        st.image(url, use_container_width=True)
            files = st.file_uploader('Optional: add listing photos to improve identification',
                type=['png', 'jpeg', 'jpg', 'webp'], accept_multiple_files=True, key=f'photos_{listing_id}')
            run_ai, _ = st.columns([2, 3])
            with run_ai:
                if st.button('Identify models with AI', key=f'ai_{listing_id}', disabled=not bool(ai_key),
                             use_container_width=True):
                    try:
                        with st.spinner('Reviewing listing description and available photos…'):
                            models, warnings = identify_models(listing, ai_key, [f.getvalue() for f in files])
                        if models:
                            listing['models'] = models
                            listing['revision'] += 1
                            listing['model_draft'] = models
                            st.session_state.ai_warnings[listing_id] = warnings
                            st.success(f'Identified {len(models)} model types. Review and correct the inventory below.')
                        else:
                            st.warning('No identifiable models found. Enter them manually below.')
                    except Exception as exc:
                        st.error('AI extraction was unsuccessful: ' + str(exc)[:350])
            if not ai_key:
                st.caption('Connect an OpenAI API key for AI-assisted extraction, or enter the model list directly.')
            for warning in st.session_state.ai_warnings.get(listing_id, []):
                st.caption('Review: ' + warning)
            st.markdown('**Inventory — edit model names and quantities before valuing**')
            st.caption('Count = individual models for “models”, or complete kits for “boxes”. Example: 5 Intercessors from a box of 10 = 5 models; a sealed box with unknown contents = 1 box.')
            frame = pd.DataFrame(listing.get('model_draft', listing['models']), columns=MODEL_COLUMNS).fillna('')
            edited = st.data_editor(frame, num_rows='dynamic', hide_index=True,
                use_container_width=True, key=f'inventory_{listing_id}_{listing["revision"]}',
                column_config={
                    'model_name': st.column_config.TextColumn('Model / kit name', width='large'),
                    'quantity': st.column_config.NumberColumn('Count', min_value=1, max_value=10000, step=1),
                    'basis': st.column_config.SelectboxColumn('Count basis', options=['models', 'boxes']),
                    'confidence': st.column_config.SelectboxColumn('Confidence', options=['manual', 'high', 'medium', 'low']),
                    'notes': st.column_config.TextColumn('Notes', width='medium'),
                })
            listing['model_draft'] = edited.fillna('').to_dict('records')
            listing['models'] = sanitise_models(listing['model_draft'])

with tab_prices:
    st.subheader('Wayland Games price catalogue')
    st.write('Upload a CSV of Wayland prices you have checked or have permission to use. This is a reusable lookup database — the tool does not scrape the retailer.')
    st.caption('For every product, enter the price of the complete retail kit and the number of miniatures/models that kit contains. A vehicle or single character is generally 1 model per box.')
    uploaded = st.file_uploader('Import Wayland price catalogue (CSV)', type=['csv'], key='catalogue_upload')
    if uploaded is not None:
        digest = hashlib.sha256(uploaded.getvalue()).hexdigest()
        if digest != st.session_state.get('catalogue_blob_digest'):
            try:
                records, warnings = parse_catalogue_csv(uploaded.getvalue())
                if not records:
                    st.warning('The CSV did not contain any valid products. Existing catalogue retained.')
                else:
                    st.session_state.catalogue = records
                    st.session_state.catalogue_blob_digest = digest
                    st.session_state.catalogue_rev += 1
                    st.session_state.match_overrides = {}
                    st.success(f'Imported {len(records):,} Wayland price records.')
                for warning in warnings[:12]:
                    st.warning(warning)
            except ValueError as exc:
                st.error(str(exc))
    st.download_button('Download blank CSV template', data=catalogue_csv([]),
        file_name='wayland_catalogue_template.csv', mime='text/csv')
    st.markdown(f'**Products in catalogue: {len(st.session_state.catalogue):,}**')
    if st.session_state.catalogue:
        filter_text = st.text_input('Filter catalogue', placeholder='Search product name…')
        filtered = [p for p in st.session_state.catalogue if filter_text.lower() in p['product_name'].lower()][:200]
        st.dataframe(pd.DataFrame(filtered, columns=CATALOGUE_COLUMNS),
            use_container_width=True, hide_index=True)
        if len(st.session_state.catalogue) > 200:
            st.caption('Showing up to 200 products; all catalogue products are used for matching.')
    st.markdown('**Add or replace a Wayland catalogue entry**')
    with st.form('new_product', clear_on_submit=True):
        c1, c2, c3 = st.columns([2.4, 1, 1])
        with c1:
            new_name = st.text_input('Product name', placeholder='e.g. Intercessors')
        with c2:
            new_price = st.number_input('Wayland kit price (£)', min_value=0., step=1., format='%.2f')
        with c3:
            new_pack = st.number_input('Models per kit', min_value=1, step=1, value=1)
        new_url = st.text_input('Wayland product page URL (optional)', placeholder='https://www.waylandgames.co.uk/…')
        submitted = st.form_submit_button('Save product price', type='primary')
        if submitted:
            records, warnings = clean_catalogue([dict(product_name=new_name, price_gbp=new_price,
                models_per_box=new_pack, product_url=new_url, checked_on=date.today().isoformat())])
            if warnings or not records:
                st.error('Please provide a product name, positive kit price, valid pack size and an optional full URL. ' + (warnings[0] if warnings else ''))
            else:
                entry = records[0]
                from core import normalise_name
                st.session_state.catalogue = [r for r in st.session_state.catalogue if normalise_name(r['product_name']) != normalise_name(new_name)] + records
                st.session_state.catalogue_rev += 1
                st.session_state.match_overrides = {}
                st.success(f'Saved {entry["product_name"]} at £{entry["price_gbp"]} per kit.')
    st.download_button('Download / back up current catalogue',
        data=catalogue_csv(st.session_state.catalogue),
        file_name='my_wayland_prices.csv', mime='text/csv', use_container_width=True)
    st.caption('The catalogue is kept in your browser session unless you save the CSV. Re-import it next session or bundle an authorised CSV in a hosted deployment.')

with tab_results:
    st.subheader('Resale purchase ceilings')
    if not st.session_state.listings:
        st.info('Add at least one eBay listing on the first tab.')
    if not st.session_state.catalogue:
        st.warning('Add Wayland Games price records on the second tab to calculate values.')
    st.caption('A match is automatic only when the product name is sufficiently unambiguous. Review each match, especially multipart kits, old sculpts, conversions and mixed lots.')
    summaries = []
    details = []
    catalogue = st.session_state.catalogue
    for listing_id, listing in st.session_state.listings.items():
        st.divider()
        st.markdown(f'### {listing["title"][:135]}')
        if listing.get('is_demo'):
            st.warning('ILLUSTRATIVE DEMO — these are not real retailer or marketplace prices.')
        if listing.get('url'):
            st.markdown(f'[View eBay item]({listing["url"]})')
        if not listing['models']:
            st.warning('No miniatures are listed. Go to the first tab and identify or enter the models.')
        selected = []
        for k, model in enumerate(listing['models']):
            match = match_catalogue(model['model_name'], catalogue)
            match_key = f'{listing_id}__{k}__{listing["revision"]}'
            prev = st.session_state.match_overrides.get(match_key, match['auto'])
            search_term = st.text_input(f'Catalogue search for {model["model_name"]}',
                placeholder='Optional: type an alternative product name',
                key=f'query_{listing_id}_{k}_{listing["revision"]}') if catalogue else ''
            candidates = match_catalogue(search_term, catalogue)['alternatives'] if search_term.strip() else match['alternatives']
            options = [None] + list(dict.fromkeys(([prev] if prev is not None else []) + candidates))
            options = [i for i in options if i is None or (isinstance(i, int) and i < len(catalogue))]
            def product_label(i):
                if i is None:
                    return 'Not matched — exclude from confirmed valuation'
                c = catalogue[i]
                return f'{c["product_name"]} · £{c["price_gbp"]} / {c["models_per_box"]} model(s)'
            c1, c2 = st.columns([1.1, 2.4], vertical_alignment='bottom')
            with c1:
                st.markdown(f'**{model["quantity"]} × {model["model_name"]}**')
                st.caption(f'{model.get("basis", "models")} · identification: {model.get("confidence", "manual")}')
                if model.get('notes'):
                    st.caption(model['notes'])
                st.markdown(f'[Search Wayland products]({retailer_search_url(model["model_name"])})')
            with c2:
                if not catalogue:
                    st.caption('Add price records in the previous tab.')
                    choice = None
                else:
                    choice = st.selectbox('Matching Wayland product', options=options, index=options.index(prev) if prev in options else 0,
                        format_func=product_label, key=f'match_{listing_id}_{k}_{listing["revision"]}_{st.session_state.catalogue_rev}',
                        label_visibility='collapsed')
                    if choice is None and match['suggestion'] is not None:
                        st.caption(f'Possible match (not accepted automatically): {product_label(match["suggestion"])}')
                    if choice is not None and catalogue[choice].get('product_url'):
                        st.markdown(f'[Wayland source page]({catalogue[choice]["product_url"]})')
                selected.append(choice)
                st.session_state.match_overrides[match_key] = choice
        v = valuation(listing, catalogue, selected)
        st.write('')
        met1, met2, met3, met4 = st.columns(4)
        with met1:
            st.metric('Wayland retail value', money_str(v['wayland_total']) if v['complete'] else 'Incomplete')
        with met2:
            st.metric('Maximum purchase (49%)', money_str(v['ceiling']) if v['ceiling'] is not None else '—')
        with met3:
            st.metric('Current eBay cost (with postage)', money_str(v['ebay_cost']) if v['ebay_cost'] is not None else 'Unconfirmed')
        with met4:
            st.metric('Difference to buying limit', f'{v["difference"]:+.2f} GBP' if v['difference'] is not None else '—')
        if v['status'] != 'Incomplete':
            if v['difference'] > 0:
                st.success(f'**Under your purchase ceiling by {money_str(v["difference"])}.**')
            elif v['difference'] == 0:
                st.info('Exactly at your maximum purchase ceiling.')
            else:
                st.error(f'**Over your purchase ceiling by {money_str(abs(v["difference"]))}.**')
        else:
            st.warning(f'Unable to make a complete purchase decision. {v["remaining"]} model type(s) lack a Wayland match, or eBay price/postage is unconfirmed. Matched Wayland subtotal: {money_str(v["matched_subtotal"])}.')
        if v['lines']:
            display_rows = [{
                'Miniature / kit': r['model_name'], 'Count': r['quantity'], 'Basis': r['basis'],
                'Wayland kit': r['matched_product'] or '—',
                'Wayland kit price': money_str(r['wayland_kit_price']) if r['matched_product'] else '—',
                'Models per kit': r['models_per_box'] or '—',
                'Retail equivalent': money_str(r['line_wayland_value_gbp']) if r['line_wayland_value_gbp'] is not None else '—'
            } for r in v['lines']]
            st.dataframe(pd.DataFrame(display_rows), hide_index=True, use_container_width=True)
        summaries.append(dict(ebay_item_id=listing_id, ebay_url=listing.get('url', ''),
            listing_title=listing['title'], ebay_price_gbp=listing.get('price_gbp'),
            shipping_gbp=listing.get('shipping_gbp'), shipping_verified=listing.get('shipping_known'),
            ebay_total_gbp=v['ebay_cost'], matched_wayland_subtotal_gbp=v['matched_subtotal'],
            confirmed_wayland_value_gbp=v['wayland_total'], max_purchase_49pct_gbp=v['ceiling'],
            difference_gbp=v['difference'], decision=v['status'], missing_model_types=v['remaining']))
        for r in v['lines']:
            details.append(dict(ebay_item_id=listing_id, ebay_url=listing.get('url', ''),
                listing_title=listing['title'], model_name=r['model_name'],
                quantity=r['quantity'], count_basis=r['basis'], identification_confidence=r['confidence'],
                wayland_product=r['matched_product'], wayland_kit_price_gbp=r['wayland_kit_price'],
                models_per_box=r['models_per_box'], retail_equivalent_gbp=r['line_wayland_value_gbp'],
                product_url=r['product_url'], notes=r['notes']))
    if summaries:
        st.divider()
        st.subheader('All listings')
        overview = pd.DataFrame(summaries)
        st.dataframe(overview[['listing_title', 'ebay_total_gbp', 'confirmed_wayland_value_gbp',
                              'max_purchase_49pct_gbp', 'difference_gbp', 'decision']],
                     use_container_width=True, hide_index=True)
        d1, d2 = st.columns(2)
        with d1:
            st.download_button('Export listing decisions CSV', data=render_csv(summaries),
                file_name='warhammer_listing_decisions.csv', mime='text/csv', use_container_width=True)
        with d2:
            st.download_button('Export model-level analysis CSV', data=render_csv(details),
                file_name='warhammer_models_detail.csv', mime='text/csv', use_container_width=True,
                disabled=not bool(details))
    st.caption('Retail-equivalent value is a pricing benchmark, not a guaranteed resale figure. Costs such as eBay resale fees, paint stripping, postage to buyers and breakages are not included in the 49% calculation.')
