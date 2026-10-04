"""
Orchestrates the pipeline and writes output
"""
import os
import json
import argparse
import pandas as pd
import logging
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

from .sourcing import find_candidate_domains, fetch_archived_html
from .shopify_detect import is_shopify_store
from .india_detect import is_india_based
from .extract import extract_store_data
from .dedup import deduplicate_data
from .http_utils import default_client, RobotsBlockedError
from .shopify_meta import fetch_shopify_meta, meta_is_india, resolves

logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def save_checkpoint(data, filepath):
    if not data: return
    pd.DataFrame(data).to_csv(filepath, index=False)

def calculate_miss_rates(data_records):
    if not data_records: return {}
    total = len(data_records)
    fields = ['emails_found', 'phones_found', 'socials_found', 'category_found', 'tagline_found', 'logo_url_found', 'state_found']
    rates = {}
    for f in fields:
        found_count = sum(1 for r in data_records if r.get(f))
        rates[f.replace('_found', '')] = ((total - found_count) / total) * 100
    return rates


def process_candidate(item):
    """Returns (status, payload). Status is one of:
    dead, blocked, failed_html, not_shopify, not_india, borderline, success
    """
    domain = item['domain']
    source = item['source']
    url = f"https://{domain}"
    warc_metadata = item.get('warc_metadata')

    # 0. Cheap DNS pre-filter: skip unresolvable hosts before any HTTP work
    if not resolves(domain):
        return ('dead', None)

    # 1. Authoritative path: Shopify /meta.json gives the store's configured country
    try:
        meta = fetch_shopify_meta(domain)
    except RobotsBlockedError as e:
        return ('blocked', {'domain': domain, 'reason': str(e)})

    if meta:
        india = meta_is_india(meta)
        if india is False:
            return ('not_india', None)
        if india is True:
            page_html = ""
            try:
                resp = default_client.get(url)
                if resp is not None and resp.status_code == 200:
                    page_html = resp.text
            except RobotsBlockedError as e:
                return ('blocked', {'domain': domain, 'reason': str(e)})
            except Exception:
                pass
            data = extract_store_data(domain, page_html or None, meta=meta)
            data.update({
                'source': source,
                'india_signal': f"meta.json country=IN ({meta.get('province') or 'n/a'})",
                'india_confidence': 100,
                'whois_shielded': None,
            })
            return ('success', data)
        # india is None -> fall through to heuristic scoring

    # 2. Legacy heuristic path (meta.json unavailable)
    page_html = ""
    if warc_metadata:
        page_html = fetch_archived_html(warc_metadata)
    if not page_html:
        try:
            resp = default_client.get(url)
            if resp is not None and resp.status_code == 200:
                page_html = resp.text
        except RobotsBlockedError as e:
            return ('blocked', {'domain': domain, 'reason': str(e)})
        except Exception:
            pass

    if not page_html:
        return ('failed_html', None)

    shopify_result = is_shopify_store(domain, page_html)
    if shopify_result['confidence'] < 40 and not meta:
        return ('not_shopify', None)

    india_result = is_india_based(domain, page_html)
    conf = india_result['confidence']

    if india_result['conflict_flag'] or (20 <= conf <= 60):
        return ('borderline', {'url': url, 'confidence': conf, 'evidence': "; ".join(india_result['evidence'])})
    if conf < 20:
        return ('not_india', None)

    data = extract_store_data(domain, page_html, meta=meta)
    data.update({
        'source': source,
        'india_signal': "; ".join(india_result['evidence'])[:300],
        'india_confidence': conf,
        'whois_shielded': india_result.get('whois_shielded', False),
    })
    return ('success', data)


def run_pipeline(limit=None, use_serper=True, use_tranco=True, use_static=True, workers=40):
    print("Starting pipeline...\n")
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
    os.makedirs(output_dir, exist_ok=True)
    
    checkpoint_file = os.path.join(output_dir, 'checkpoint.csv')
    borderline_file = os.path.join(output_dir, 'borderline_review.csv')
    blocked_file = os.path.join(output_dir, 'blocked_by_robots.csv')
    results_csv = os.path.join(output_dir, 'results.csv')
    results_json = os.path.join(output_dir, 'results.json')
    
    print("Sourcing candidates...")
    candidates_data = find_candidate_domains(
        csv_filepath=os.path.join(output_dir, 'seed_list.csv'),
        use_serper=use_serper, use_tranco=use_tranco, use_static=use_static,
    )
    if limit and limit > 0:
        candidates_data = candidates_data[:limit]
        
    if not candidates_data:
        return

    import concurrent.futures
    import time
    
    total_candidates = len(candidates_data)
    print(f"Processing {total_candidates} candidates with {workers} workers...")
    counts = Counter()
    raw_data, borderline_cases, blocked_cases = [], [], []
    start = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {executor.submit(process_candidate, item): item for item in candidates_data}
        
        for i, future in enumerate(concurrent.futures.as_completed(future_to_item), 1):
            try:
                status, result = future.result()
            except Exception as e:
                status, result = 'error', None
                logger.debug(f"{future_to_item[future]['domain']}: {e}")
            counts[status] += 1

            if status == 'blocked':
                blocked_cases.append(result)
            elif status == 'borderline':
                borderline_cases.append(result)
            elif status == 'success':
                raw_data.append(result)
                if len(raw_data) % 25 == 0:
                    save_checkpoint(raw_data, checkpoint_file)

            if i % 100 == 0:
                print(f"Processed {i}/{total_candidates} | confirmed {counts['success']} | "
                      f"{time.time() - start:.0f}s elapsed")
            
    pd.DataFrame(borderline_cases, columns=['url', 'confidence', 'evidence']).to_csv(borderline_file, index=False)
    pd.DataFrame(blocked_cases, columns=['domain', 'reason']).to_csv(blocked_file, index=False)
        
    print("\nDeduplicating extracted data...")
    final_data = deduplicate_data(raw_data)
    final_count = len(final_data)
    
    if final_data:
        filtered_data = []
        for d in final_data:
            filtered_data.append({
                'domain_url': d.get('domain_url'),
                'emails': d.get('emails'),
                'phones': d.get('phones'),
                'socials': d.get('socials'),
                'category': d.get('category'),
                'tagline': d.get('tagline'),
                'logo_url': d.get('logo_url'),
                'state': d.get('state'),
                'foreign_brand_india_storefront': d.get('foreign_brand_india_storefront', False)
            })
            
        df = pd.DataFrame(filtered_data)
        df_csv = df.copy()
        
        def format_list(val):
            return str(val) if isinstance(val, list) and val else ""
            
        def format_dict(val):
            return str(val) if isinstance(val, dict) and val else ""
            
        df_csv['emails'] = df_csv['emails'].apply(format_list)
        df_csv['phones'] = df_csv['phones'].apply(format_list)
        df_csv['socials'] = df_csv['socials'].apply(format_dict)
        
        import numpy as np
        df_csv.replace("", np.nan, inplace=True)
        
        df_csv.to_csv(results_csv, index=False)
        with open(results_json, 'w', encoding='utf-8') as f:
            json.dump(filtered_data, f, indent=4, default=str)

    shopify_confirmed = counts['success'] + counts['borderline'] + counts['not_india']
    print("\n" + "="*45)
    print("           PIPELINE SUMMARY")
    print("="*45)
    print(f"Total candidates checked: {total_candidates}")
    print(f"Runtime:                  {(time.time() - start) / 60:.1f} min")
    print(f"Dead (no DNS):            {counts['dead']}")
    print(f"Fetch failed:             {counts['failed_html'] + counts['error']}")
    print(f"Not Shopify:              {counts['not_shopify']}")
    print(f"Shopify confirmed:        {shopify_confirmed}")
    print(f"India confirm rate:       {(counts['success'] / shopify_confirmed * 100) if shopify_confirmed else 0:.1f}% ({counts['success']})")
    print(f"Final Count (post-dedup): {final_count}")
    print(f"Borderline / Review:      {len(borderline_cases)}")
    print(f"Blocked by robots.txt:    {len(blocked_cases)}")
    
    if final_data:
        print("\nPer-field Miss Rates:")
        for field, miss_rate in calculate_miss_rates(final_data).items():
            print(f"  - {field:<12}: {miss_rate:5.1f}%")
    print("="*45)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=40)
    parser.add_argument("--no-serper", action="store_true", help="Skip Serper API sourcing")
    parser.add_argument("--no-tranco", action="store_true", help="Skip Tranco + DNS sourcing")
    parser.add_argument("--no-static", action="store_true", help="Skip live listicle scraping")
    args = parser.parse_args()
    run_pipeline(limit=args.limit, use_serper=not args.no_serper, use_tranco=not args.no_tranco,
                 use_static=not args.no_static, workers=args.workers)
