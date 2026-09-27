"""
Candidate domain discovery and archived content fetching
"""
import os
import json
import logging
import gzip
from urllib.parse import urlparse
import pandas as pd
import requests
from .http_utils import default_client

logger = logging.getLogger(__name__)

def clean_domain(url: str) -> str:
    if not url.startswith(('http://', 'https://')):
        url = f"https://{url}"
    return urlparse(url).netloc

def fetch_archived_html(warc_metadata: dict) -> str:
    """Pulls HTML directly from Common Crawl's S3 bucket using offset/length."""
    if not warc_metadata or not warc_metadata.get('filename'):
        return ""
        
    url = f"https://data.commoncrawl.org/{warc_metadata['filename']}"
    offset = int(warc_metadata['offset'])
    length = int(warc_metadata['length'])
    headers = {'Range': f"bytes={offset}-{offset + length - 1}"}
    
    try:
        resp = default_client.get(url, headers=headers, ignore_robots=True)
        if resp and resp.status_code in [200, 206]:
            raw_warc = gzip.decompress(resp.content)
            # Split WARC header, HTTP header, Body
            parts = raw_warc.split(b'\r\n\r\n', 2)
            if len(parts) >= 3:
                return parts[2].decode('utf-8', errors='replace')
    except Exception as e:
        logger.error(f"Error fetching WARC from CC: {e}")
    return ""

def _paginate_cc(url_query: str, page_limit: int, checkpoint_name: str) -> dict:
    candidates = {}
    index_name = "CC-MAIN-2024-10-index"
    cc_api_url = f"https://index.commoncrawl.org/{index_name}"
    
    import time
    import random
    unique_domains = set()
    start_time = time.time()
    
    print(f"Paginating Common Crawl index: {index_name} for '{url_query}' up to {page_limit} pages...")
    
    consecutive_errors = 0
    
    for page in range(page_limit):
        page_success = False
        attempt = 0
        max_retries = 10  # Increased limit to allow recovering from rate limits
        
        while not page_success and attempt < max_retries:
            attempt += 1
            try:
                print(f"  -> Fetching page {page}/{page_limit} (Attempt {attempt})...", end="", flush=True)
                response = default_client.get(
                    f"{cc_api_url}?url={url_query}&output=json&page={page}", 
                    ignore_robots=True,
                    timeout=60
                )
                if response and response.status_code == 200:
                    print(" Success!")
                    new_domains_this_page = 0
                    for line in response.text.strip().split('\n'):
                        if not line: continue
                        try:
                            data = json.loads(line)
                            domain = clean_domain(data.get('url', ''))
                            # Platform blocklist
                            if domain in ['apps.shopify.com', 'community.shopify.com', 'help.shopify.com', 'www.myshopify.com', 'myshopify.com', 'cdn.shopify.com', 'checkout.shopify.com']:
                                continue
                            if domain and domain not in candidates:
                                candidates[domain] = {
                                    'source': f'Common Crawl ({url_query})',
                                    'warc_metadata': {
                                        'filename': data.get('filename'),
                                        'offset': data.get('offset'),
                                        'length': data.get('length')
                                    }
                                }
                                unique_domains.add(domain)
                                new_domains_this_page += 1
                        except json.JSONDecodeError:
                            pass
                            
                    print(f"     Found {new_domains_this_page} new unique domains. Running total: {len(unique_domains)}")
                    page_success = True
                    consecutive_errors = 0  # Reset on success
                    break
                else:
                    print(f" Failed (Status {response.status_code if response else 'None'})")
                    consecutive_errors += 1
            except Exception as e:
                print(f" Error: {e}")
                consecutive_errors += 1
                
            if not page_success:
                if consecutive_errors >= 2:
                    backoff = random.uniform(30.0, 60.0)
                    print(f"     [!] 2+ consecutive errors. Backing off for {backoff:.1f}s before resuming...")
                    time.sleep(backoff)
                else:
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        
        if not page_success:
            print(f"     [!] Page {page} completely failed after {attempt} attempts. Moving on.")
                
        # Checkpoint every 20 pages
        if (page + 1) % 20 == 0:
            checkpoint_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', f'{checkpoint_name}.json')
            os.makedirs(os.path.dirname(checkpoint_file), exist_ok=True)
            try:
                with open(checkpoint_file, 'w') as f:
                    json.dump(list(unique_domains), f)
                print(f"     [Checkpoint Saved: {len(unique_domains)} domains]")
            except Exception as e:
                print(f"     [Failed to save checkpoint: {e}]")
            
        # Jitter delay between pages to avoid predictable rate triggers
        time.sleep(random.uniform(1.0, 3.0))
        
    elapsed = time.time() - start_time
    print(f"CC Pagination complete for {url_query}: {len(unique_domains)} domains in {elapsed:.1f}s")
    return candidates

def source_from_common_crawl_myshopify(page_limit: int = 200) -> dict:
    return _paginate_cc("*.myshopify.com/*", page_limit, "cc_checkpoint_myshopify")

def source_from_common_crawl_in(page_limit: int = 200) -> dict:
    return _paginate_cc("*.in/*", page_limit, "cc_checkpoint_in")

def get_serper_query_count() -> int:
    tracker_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'serper_usage.json')
    if os.path.exists(tracker_file):
        try:
            with open(tracker_file, 'r') as f:
                return json.load(f).get('queries_used', 0)
        except Exception:
            pass
    return 0

def increment_serper_query_count(used: int = 1) -> int:
    tracker_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'serper_usage.json')
    count = get_serper_query_count() + used
    os.makedirs(os.path.dirname(tracker_file), exist_ok=True)
    try:
        with open(tracker_file, 'w') as f:
            json.dump({'queries_used': count}, f)
    except Exception:
        pass
    return count

def source_from_serper() -> dict:
    candidates = {}
    api_key = os.environ.get('SERPER_API_KEY')
    if not api_key:
        return candidates
        
    categories = [
        "skincare", "apparel", "home decor", "electronics", "jewelry", 
        "furniture", "beauty", "cosmetics", "fashion", "shoes", 
        "activewear", "wellness", "supplements", "ethnic wear", 
        "organic food", "beverages", "accessories", "toys", "pet supplies"
    ]
    
    current_usage = get_serper_query_count()
    if current_usage >= 2500:
        return candidates
        
    success_count = 0
    reject_count = 0
        
    for cat in categories:
        if get_serper_query_count() >= 2500:
            break
            
        # Use natural language to bypass Free Tier advanced operator blocks
        query = f'{cat} shopify store india buy online'
        try:
            url = "https://google.serper.dev/search"
            payload = json.dumps({"q": query, "num": 100})
            headers = {
              'X-API-KEY': api_key,
              'Content-Type': 'application/json'
            }
            response = requests.post(url, headers=headers, data=payload, timeout=15)
            increment_serper_query_count(1)
            
            if response.status_code == 200:
                success_count += 1
                data = response.json()
                for result in data.get('organic', []):
                    domain = clean_domain(result.get('link', ''))
                    if domain and domain not in candidates:
                        candidates[domain] = {'source': f"Serper API: {cat}"}
            elif response.status_code == 400:
                reject_count += 1
                logger.error(f"Serper API rejected query '{query}' (400 Bad Request): {response.text}")
            else:
                reject_count += 1
                logger.error(f"Serper API returned {response.status_code} for query '{query}': {response.text}")
        except Exception as e:
            reject_count += 1
            logger.error(f"Serper API error for query '{query}': {e}")
            
    logger.info(f"Serper API run complete. Successful: {success_count}, Rejected: {reject_count}.")
    return candidates

def source_from_csv(filepath: str) -> dict:
    candidates = {}
    if not os.path.exists(filepath):
        return candidates
    try:
        df = pd.read_csv(filepath)
        url_col = None
        for col in df.columns:
            if 'url' in col.lower() or 'domain' in col.lower():
                url_col = col
                break
        if url_col:
             for url in df[url_col].dropna():
                 domain = clean_domain(str(url))
                 if domain and domain not in candidates:
                     candidates[domain] = {'source': 'CSV Import'}
    except Exception as e:
        logger.error(f"Error reading CSV {filepath}: {e}")
    return candidates

def source_from_static_lists() -> dict:
    candidates = {}
    urls = [
        "https://www.magenest.com/en/shopify-stores-in-india/",
        "https://qikink.com/blog/top-d2c-brands-in-india/",
        "https://www.limechat.ai/blogs/top-100-d2c-brands-in-india",
        "https://d2cbazaar.com/blog/top-d2c-brands-in-india/",
        "https://www.clickpost.ai/blog/d2c-brands-in-india",
        "https://www.eshopbox.com/blog/top-d2c-brands-india"
    ]
    
    # Common domains to ignore
    blocklist = {
        'facebook.com', 'instagram.com', 'twitter.com', 'youtube.com', 'linkedin.com',
        'pinterest.com', 'tiktok.com', 'google.com', 'shopify.com', 'myshopify.com',
        'amazon.com', 'amazon.in', 'flipkart.com', 'myntra.com', 'nykaa.com',
        'wikipedia.org', 'apple.com', 'play.google.com', 'apps.apple.com', 'whatsapp.com',
        'magenest.com', 'qikink.com', 'limechat.ai', 'd2cbazaar.com', 'clickpost.ai', 'eshopbox.com',
        'forbes.com', 'bloomberg.com', 'techcrunch.com', 'yourstory.com', 'inc42.com'
    }
    
    import re
    domain_pattern = re.compile(r'([a-zA-Z0-9-]+\.(?:com|in|co\.in))')
    
    logger.info("Scraping static lists for candidate domains...")
    for url in urls:
        try:
            resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}, timeout=15)
            if resp.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, 'html.parser')
                
                # 1. Check hrefs
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if href.startswith('http'):
                        try:
                            netloc = urlparse(href).netloc
                            netloc = netloc.replace('www.', '').lower()
                            if netloc.endswith(('.com', '.in', '.co.in')) and netloc not in blocklist:
                                if netloc not in candidates:
                                    candidates[netloc] = {'source': f'Static List: {urlparse(url).netloc}'}
                        except Exception:
                            pass
                            
                # 2. Check text body
                text = soup.get_text()
                matches = domain_pattern.findall(text)
                for match in matches:
                    match = match.lower().replace('www.', '')
                    if match not in blocklist:
                        if match not in candidates:
                            candidates[match] = {'source': f'Static List: {urlparse(url).netloc}'}
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            
    return candidates

def find_candidate_domains(csv_filepath: str = None) -> list:
    """Returns a list of dicts: [{'domain': x, 'source': y, 'warc_metadata': z}]"""
    all_candidates = {} 
    
    if csv_filepath:
        logger.info(f"Loading from CSV: {csv_filepath}")
        csv_domains = source_from_csv(csv_filepath)
        for dom, info in csv_domains.items():
            if dom not in all_candidates:
                all_candidates[dom] = info
                
    logger.info("Fetching from large dynamic listicles CSV...")
    large_candidates = source_from_csv(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'large_static_candidates.csv'))
    for dom, info in large_candidates.items():
        if dom not in all_candidates:
            all_candidates[dom] = info
            
    logger.info("Fetching from sister brands CSV...")
    sister_candidates = source_from_csv(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'sister_brands.csv'))
    for dom, info in sister_candidates.items():
        if dom not in all_candidates:
            all_candidates[dom] = info
            
    logger.info("Fetching from Serper API...")
    serper_domains = source_from_serper()
    for dom, info in serper_domains.items():
        if dom not in all_candidates:
            all_candidates[dom] = info
            
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
    os.makedirs(output_dir, exist_ok=True)
    candidates_file = os.path.join(output_dir, 'candidates.csv')
    
    records = []
    for dom, info in all_candidates.items():
        record = {'domain': dom, 'source': info['source']}
        if 'warc_metadata' in info:
            record['warc_metadata'] = info['warc_metadata']
        records.append(record)
    
    if records:
        df = pd.DataFrame([{'domain': r['domain'], 'source': r['source']} for r in records])
        df.to_csv(candidates_file, index=False)
        
    return records
