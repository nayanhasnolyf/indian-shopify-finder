"""
Orchestrates the pipeline and writes output
"""
import os
import json
import argparse
import pandas as pd
import logging
from dotenv import load_dotenv

load_dotenv()

from .sourcing import find_candidate_domains, fetch_archived_html
from .shopify_detect import is_shopify_store
from .india_detect import is_india_based
from .extract import extract_store_data
from .dedup import deduplicate_data
from .http_utils import default_client, RobotsBlockedError

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
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

def run_pipeline(limit=None):
    print("Starting pipeline...\n")
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
    os.makedirs(output_dir, exist_ok=True)
    
    checkpoint_file = os.path.join(output_dir, 'checkpoint.csv')
    borderline_file = os.path.join(output_dir, 'borderline_review.csv')
    blocked_file = os.path.join(output_dir, 'blocked_by_robots.csv')
    results_csv = os.path.join(output_dir, 'results.csv')
    results_json = os.path.join(output_dir, 'results.json')
    
    print("Sourcing candidates...")
    candidates_data = find_candidate_domains(csv_filepath="data/seed_list.csv")
    if limit and limit > 0:
        candidates_data = candidates_data[:limit]
        
    if not candidates_data:
        return

    import concurrent.futures
    import threading
    
    total_candidates = len(candidates_data)
    shopify_confirmed, india_confirmed = 0, 0
    raw_data, borderline_cases, blocked_cases = [], [], []
    
    lock = threading.Lock()
    processed_count = 0
    
    def process_candidate(item):
        domain = item['domain']
        source = item['source']
        url = f"https://{domain}"
        warc_metadata = item.get('warc_metadata')
        
        page_html = ""
        is_live_fetch = False
        
        # 1. Fetch HTML (Prefer Archive)
        if warc_metadata:
             page_html = fetch_archived_html(warc_metadata)
             
        # Fallback to Live Fetch
        if not page_html:
             is_live_fetch = True
             try:
                 resp = default_client.get(url)
                 if resp and resp.status_code == 200:
                     page_html = resp.text
             except RobotsBlockedError as e:
                 return ('blocked', {'domain': domain, 'reason': str(e)})
                 
        if not page_html:
             return ('failed_html', None)
             
        # 2. Check Shopify
        shopify_result = is_shopify_store(domain, page_html)
        if shopify_result['confidence'] < 40:
            return ('not_shopify', None)
            
        # 3. Check India
        india_result = is_india_based(domain, page_html)
        conf = india_result['confidence']
        
        if india_result['conflict_flag'] or (20 <= conf <= 60):
            return ('borderline', {'url': url, 'confidence': conf, 'evidence': "; ".join(india_result['evidence'])})
            
        if conf < 20:
            return ('not_india', None)
            
        # 4. Extract
        data = extract_store_data(domain, page_html)
        data['whois_shielded'] = india_result.get('whois_shielded', False)
        return ('success', data)

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        future_to_item = {executor.submit(process_candidate, item): item for item in candidates_data}
        
        for future in concurrent.futures.as_completed(future_to_item):
            with lock:
                processed_count += 1
                if processed_count % 50 == 0:
                    print(f"Processed {processed_count}/{total_candidates}...")
                    
            try:
                status, result = future.result()
                with lock:
                    if status == 'blocked':
                        blocked_cases.append(result)
                    elif status == 'not_shopify':
                        pass
                    elif status == 'borderline':
                        shopify_confirmed += 1
                        borderline_cases.append(result)
                    elif status == 'not_india':
                        shopify_confirmed += 1
                    elif status == 'success':
                        shopify_confirmed += 1
                        india_confirmed += 1
                        raw_data.append(result)
                        
                        if len(raw_data) % 10 == 0:
                            save_checkpoint(raw_data, checkpoint_file)
            except Exception as e:
                pass
            
    if borderline_cases: pd.DataFrame(borderline_cases).to_csv(borderline_file, index=False)
    if blocked_cases: pd.DataFrame(blocked_cases).to_csv(blocked_file, index=False)
        
    print("\nDeduplicating extracted data...")
    final_data = deduplicate_data(raw_data)
    final_count = len(final_data)
    
    if final_data:
        df = pd.DataFrame(final_data)
        df.to_csv(results_csv, index=False)
        with open(results_json, 'w', encoding='utf-8') as f:
            json.dump(final_data, f, indent=4)
    
    print("\n" + "="*45)
    print("           PIPELINE SUMMARY")
    print("="*45)
    print(f"Total candidates checked: {total_candidates}")
    print(f"Shopify confirm rate:     {(shopify_confirmed / total_candidates * 100) if total_candidates else 0:.1f}% ({shopify_confirmed})")
    print(f"India confirm rate:       {(india_confirmed / shopify_confirmed * 100) if shopify_confirmed else 0:.1f}% ({india_confirmed})")
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
    args = parser.parse_args()
    run_pipeline(limit=args.limit)
