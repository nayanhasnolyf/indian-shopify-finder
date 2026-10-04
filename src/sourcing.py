"""
Candidate domain discovery and archived content fetching
"""
import os
import re
import io
import json
import logging
import gzip
import zipfile
import concurrent.futures
from urllib.parse import urlparse
import pandas as pd
import requests
from .http_utils import default_client

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
BROWSER_UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}

# Domains that are never candidate stores (marketplaces, social, media, SaaS, blogs)
NOISE_DOMAINS = {
    'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'youtube.com', 'linkedin.com',
    'pinterest.com', 'tiktok.com', 'google.com', 'google.co.in', 'shopify.com', 'myshopify.com',
    'amazon.com', 'amazon.in', 'flipkart.com', 'myntra.com', 'nykaa.com', 'ajio.com', 'meesho.com',
    'tatacliq.com', 'snapdeal.com', 'jiomart.com', 'bigbasket.com', 'blinkit.com', 'swiggy.com',
    'zomato.com', 'paytm.com', 'paytmmall.com', 'indiamart.com', 'justdial.com', 'tradeindia.com',
    'wikipedia.org', 'apple.com', 'whatsapp.com', 'reddit.com', 'quora.com', 'medium.com',
    'forbes.com', 'forbesindia.com', 'bloomberg.com', 'techcrunch.com', 'yourstory.com', 'inc42.com',
    'economictimes.com', 'indiatimes.com', 'livemint.com', 'business-standard.com', 'moneycontrol.com',
    'thehindu.com', 'hindustantimes.com', 'ndtv.com', 'entrackr.com', 'vccircle.com', 'crunchbase.com',
    'tracxn.com', 'similarweb.com', 'builtwith.com', 'storeleads.app', 'wordpress.com', 'wix.com',
    'blogspot.com', 'github.com', 'gstatic.com', 'googleapis.com', 'cloudflare.com', 'bit.ly',
    'magenest.com', 'qikink.com', 'limechat.ai', 'd2cbazaar.com', 'clickpost.ai', 'eshopbox.com',
    'razorpay.com', 'shiprocket.in', 'gokwik.co', 'instamojo.com', 'dukaan.app', 'zoho.com',
    'hubspot.com', 'semrush.com', 'ahrefs.com', 'glassdoor.co.in', 'glassdoor.com', 'ambitionbox.com',
    'naukri.com', 'indeed.com', 'zaubacorp.com', 'tofler.in', 'gov.in', 'nic.in',
}
NOISE_SUFFIXES = ('.gov.in', '.nic.in', '.ac.in', '.edu.in', '.res.in', '.edu', '.gov')

def is_noise(domain: str) -> bool:
    d = domain.lower().removeprefix('www.')
    if not d or '.' not in d or d.endswith(NOISE_SUFFIXES):
        return True
    # Match the domain itself or any parent (e.g. blog.hubspot.com)
    parts = d.split('.')
    return any('.'.join(parts[i:]) in NOISE_DOMAINS for i in range(len(parts) - 1))

def clean_domain(url: str) -> str:
    if not url.startswith(('http://', 'https://')):
        url = f"https://{url}"
    return urlparse(url).netloc.lower().removeprefix('www.')

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


# ---------------------------------------------------------------------------
# Serper: store queries + listicle harvesting
# ---------------------------------------------------------------------------
SERPER_CATEGORIES = [
    "skincare", "apparel", "home decor", "electronics", "jewellery", "furniture", "beauty",
    "cosmetics", "fashion", "footwear", "activewear", "wellness", "supplements", "ethnic wear",
    "organic food", "beverages", "accessories", "toys", "pet supplies", "haircare", "saree",
    "kurta", "handloom", "ayurvedic", "coffee", "tea", "snacks", "spices", "kitchenware",
    "bags", "watches", "perfume", "men's grooming", "baby products", "kids clothing",
    "lingerie", "stationery", "handicrafts", "bedsheets", "candles", "plants", "silver jewellery",
    "sustainable fashion", "streetwear", "protein", "millets", "ghee", "dry fruits", "eyewear",
]
SERPER_STORE_TEMPLATES = [
    "{cat} brand india shop online",
    "buy {cat} online india D2C brand official website",
]
SERPER_LISTICLE_QUERIES = [
    "top indian d2c brands list", "indian shopify stores list", "best shopify stores india examples",
    "best indian {cat} brands d2c", "homegrown indian {cat} brands",
]

def _serper_search(api_key: str, query: str, num: int = 20) -> list:
    """Returns organic result links, or [] on failure. Increments the usage tracker."""
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={'X-API-KEY': api_key, 'Content-Type': 'application/json'},
            data=json.dumps({"q": query, "gl": "in", "num": num}),
            timeout=15,
        )
        increment_serper_query_count(1)
        if response.status_code == 200:
            return [r.get('link', '') for r in response.json().get('organic', [])]
        logger.error(f"Serper {response.status_code} for '{query}': {response.text[:200]}")
    except Exception as e:
        logger.error(f"Serper API error for query '{query}': {e}")
    return []

def source_from_serper(max_queries: int = None) -> dict:
    """Two query types:
    1. Store queries    -> result domains are candidates directly.
    2. Listicle queries -> result pages are scraped for outbound store links (higher yield).
    Budget is capped by SERPER_MAX_QUERIES (default 200) and the 2,500 free-tier limit.
    """
    candidates = {}
    api_key = os.environ.get('SERPER_API_KEY')
    if not api_key:
        return candidates
    if max_queries is None:
        max_queries = int(os.environ.get('SERPER_MAX_QUERIES', 200))

    stores = [('store', t.format(cat=c)) for c in SERPER_CATEGORIES for t in SERPER_STORE_TEMPLATES]
    lists = []
    for t in SERPER_LISTICLE_QUERIES:
        if '{cat}' in t:
            lists.extend(('listicle', t.format(cat=c)) for c in SERPER_CATEGORIES)
        else:
            lists.append(('listicle', t))
    # Interleave so a small budget still covers both kinds
    queries = [q for pair in zip(lists, stores) for q in pair]
    queries += stores[len(lists):] + lists[len(stores):]

    used = 0
    listicle_urls = set()
    for kind, query in queries:
        if used >= max_queries or get_serper_query_count() >= 2500:
            break
        links = _serper_search(api_key, query)
        used += 1
        for link in links:
            domain = clean_domain(link)
            if not domain or is_noise(domain):
                continue
            if kind == 'store':
                candidates.setdefault(domain, {'source': f"Serper: {query}"})
            else:
                listicle_urls.add(link)

    print(f"  Serper: {used} queries, {len(candidates)} direct domains, {len(listicle_urls)} listicle pages to harvest")
    harvested = harvest_pages(sorted(listicle_urls), source_prefix='Serper listicle')
    for dom, info in harvested.items():
        candidates.setdefault(dom, info)
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


# ---------------------------------------------------------------------------
# Listicle / article link harvesting
# ---------------------------------------------------------------------------
STORE_TLDS = ('.com', '.in', '.co.in', '.co', '.store', '.shop', '.net', '.org', '.life', '.farm', '.club', '.studio')

def _harvest_outbound_domains(url: str) -> set:
    """Extract outbound candidate store domains from an article/listicle page."""
    from bs4 import BeautifulSoup
    found = set()
    try:
        resp = requests.get(url, headers=BROWSER_UA, timeout=15)
        if resp.status_code != 200:
            return found
    except Exception:
        return found
    page_domain = clean_domain(url)
    soup = BeautifulSoup(resp.text, 'html.parser')
    for a in soup.find_all('a', href=True):
        href = a['href']
        if not href.startswith('http'):
            continue
        netloc = clean_domain(href)
        if netloc and netloc != page_domain and netloc.endswith(STORE_TLDS) and not is_noise(netloc):
            found.add(netloc)
    # Plain-text mentions like "mamaearth.in"
    for match in re.findall(r'\b((?:[a-z0-9-]+\.)+(?:com|in|co\.in))\b', soup.get_text(' ').lower()):
        match = match.removeprefix('www.')
        if match != page_domain and not is_noise(match):
            found.add(match)
    return found

def harvest_pages(urls: list, source_prefix: str, workers: int = 16) -> dict:
    candidates = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for url, domains in zip(urls, ex.map(_harvest_outbound_domains, urls)):
            for d in domains:
                candidates.setdefault(d, {'source': f'{source_prefix}: {clean_domain(url)}'})
    return candidates

def source_from_static_lists() -> dict:
    urls = [
        "https://www.magenest.com/en/shopify-stores-in-india/",
        "https://qikink.com/blog/top-d2c-brands-in-india/",
        "https://www.limechat.ai/blogs/top-100-d2c-brands-in-india",
        "https://d2cbazaar.com/blog/top-d2c-brands-in-india/",
        "https://www.clickpost.ai/blog/d2c-brands-in-india",
        "https://www.eshopbox.com/blog/top-d2c-brands-india",
    ]
    logger.info("Scraping static lists for candidate domains...")
    return harvest_pages(urls, source_prefix='Static List')


# ---------------------------------------------------------------------------
# Tranco top-1M + DNS: find India-TLD domains hosted on Shopify's edge IPs.
# ---------------------------------------------------------------------------
INDIA_TLDS = ('.in', '.co.in', '.net.in', '.org.in', '.firm.in', '.gen.in', '.ind.in')
TRANCO_URL = 'https://tranco-list.eu/top-1m.csv.zip'

def _load_tranco() -> list:
    cache = os.path.join(DATA_DIR, 'tranco_top1m.csv')
    if not os.path.exists(cache):
        print("  Downloading Tranco top-1M list (~10 MB)...")
        resp = requests.get(TRANCO_URL, timeout=120)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            with open(cache, 'wb') as f:
                f.write(z.read(z.namelist()[0]))
    with open(cache, encoding='utf-8') as f:
        return [line.strip().split(',', 1)[1] for line in f if ',' in line]

def source_from_tranco_dns(workers: int = 200) -> dict:
    """India-TLD domains from Tranco whose apex/www resolves to Shopify (23.227.38.x).
    Results are cached in data/tranco_shopify_in.csv so reruns are instant.
    """
    from .shopify_meta import is_shopify_dns
    cache = os.path.join(DATA_DIR, 'tranco_shopify_in.csv')
    if os.path.exists(cache):
        doms = pd.read_csv(cache)['domain'].dropna().tolist()
        return {d: {'source': 'Tranco+DNS'} for d in doms}

    try:
        domains = [d for d in _load_tranco() if d.endswith(INDIA_TLDS) and not is_noise(d)]
    except Exception as e:
        logger.error(f"Tranco download failed: {e}")
        return {}
    print(f"  Tranco: {len(domains)} India-TLD domains, DNS-checking for Shopify hosting...")

    hits = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (d, ok) in enumerate(zip(domains, ex.map(is_shopify_dns, domains)), 1):
            if ok:
                hits.append(d)
            if i % 2000 == 0:
                print(f"    DNS checked {i}/{len(domains)} | Shopify-hosted: {len(hits)}")
    pd.DataFrame({'domain': hits}).to_csv(cache, index=False)
    return {d: {'source': 'Tranco+DNS'} for d in hits}


def find_candidate_domains(csv_filepath: str = None, use_serper: bool = True,
                           use_tranco: bool = True, use_static: bool = True) -> list:
    """Returns a list of dicts: [{'domain': x, 'source': y, 'warc_metadata': z}]"""
    all_candidates = {} 

    def merge(found: dict, label: str):
        before = len(all_candidates)
        for dom, info in found.items():
            dom = dom.lower().removeprefix('www.')
            if dom and not is_noise(dom) and dom not in all_candidates:
                all_candidates[dom] = info
        print(f"  {label:<26} {len(found):>6} found, +{len(all_candidates) - before:>5} new (total {len(all_candidates)})")
    
    if csv_filepath:
        merge(source_from_csv(csv_filepath), "Seed list")
    merge(source_from_csv(os.path.join(DATA_DIR, 'large_static_candidates.csv')), "Large listicles CSV")
    merge(source_from_csv(os.path.join(DATA_DIR, 'sister_brands.csv')), "Sister brands CSV")
    if use_static:
        merge(source_from_static_lists(), "Static listicles (live)")
    if use_tranco:
        merge(source_from_tranco_dns(), "Tranco + DNS")
    if use_serper:
        merge(source_from_serper(), "Serper API")
            
    os.makedirs(DATA_DIR, exist_ok=True)
    candidates_file = os.path.join(DATA_DIR, 'candidates.csv')
    
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
