"""
Confirm a store is India-based
"""
import logging
import re
import json
import tldextract
import whois
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from .http_utils import default_client, RobotsBlockedError
from .sourcing import fetch_archived_html

logger = logging.getLogger(__name__)

# Common WHOIS privacy protection strings
PRIVACY_SHIELDS = [
    'cloudflare', 'godaddy', 'proxy', 'privacy', 'protect', 
    'whoisguard', 'namecheap', 'redacted', 'statutory', 'mask', 'domainsbyproxy'
]

INDIAN_STATES = [
    'maharashtra', 'karnataka', 'tamil nadu', 'delhi', 'gujarat', 
    'uttar pradesh', 'kerala', 'telangana', 'west bengal', 'haryana', 'punjab'
]
INDIAN_CITIES = [
    'mumbai', 'bengaluru', 'bangalore', 'new delhi', 'chennai', 'hyderabad', 'kolkata', 'pune', 'ahmedabad', 'jaipur'
]

def smart_fetch(url: str) -> str:
    """Tries to fetch from CC archive first, then live fetch as fallback."""
    # 1. Try Common Crawl Archive
    try:
        cc_api_url = "https://index.commoncrawl.org/CC-MAIN-2024-10-index"
        resp = default_client.get(f"{cc_api_url}?url={url}&output=json&limit=1", ignore_robots=True)
        if resp and resp.status_code == 200:
            lines = resp.text.strip().split('\n')
            if lines and lines[0]:
                data = json.loads(lines[0])
                warc = {
                    'filename': data.get('filename'),
                    'offset': data.get('offset'),
                    'length': data.get('length')
                }
                html = fetch_archived_html(warc)
                if html: 
                    return html
    except Exception:
        pass
    
    # 2. Live Fetch Fallback
    try:
        resp = default_client.get(url)
        if resp and resp.status_code == 200:
            return resp.text
    except RobotsBlockedError:
        pass
    except Exception:
        pass
        
    return ""

def score_html_content(page_html: str, source_name: str, current_evidence: list) -> int:
    """Scores HTML content and tracks which page contributed the winning signal."""
    confidence_added = 0
    soup = BeautifulSoup(page_html, 'html.parser')
    text_content = soup.get_text(separator=' ', strip=True)
    text_lower = text_content.lower()
    
    # Phone Numbers (+91)
    if not any("Phone number prefix +91" in e for e in current_evidence):
        if '+91' in text_content or re.search(r'(?:\+91|091)[-\s]?[6-9]\d{9}', text_content):
            confidence_added += 40
            current_evidence.append(f"Phone number prefix +91 found (Source: {source_name})")
            
    # Address patterns
    if not any("Indian location(s) found" in e for e in current_evidence):
        found_locations = [loc for loc in INDIAN_STATES + INDIAN_CITIES if loc in text_lower]
        if found_locations:
            confidence_added += 30
            current_evidence.append(f"Indian location(s) found: {', '.join(found_locations)} (Source: {source_name})")
            
    # PIN code
    if not any("6-digit PIN" in e for e in current_evidence):
        if re.search(r'\b[1-8][0-9]{5}\b', text_content):
            confidence_added += 15
            current_evidence.append(f"Possible Indian 6-digit PIN code found (Source: {source_name})")
            
    # Currency
    if not any("currency symbol" in e for e in current_evidence):
        if '₹' in text_content or 'inr' in text_lower or 'rs.' in text_lower or 'rs ' in text_lower:
            confidence_added += 30
            current_evidence.append(f"Indian currency symbol/acronym (₹, INR, Rs) found (Source: {source_name})")
            
    # HTML Lang tags
    if not any("HTML lang attribute" in e for e in current_evidence):
        html_tag = soup.find('html')
        if html_tag and html_tag.get('lang'):
            lang = html_tag.get('lang').lower()
            if 'in' in lang.split('-'):
                confidence_added += 10
                current_evidence.append(f"HTML lang attribute indicates India ({lang}) (Source: {source_name})")

    return confidence_added

def is_india_based(domain: str, page_html: str) -> dict:
    confidence = 0
    evidence = []
    conflict_flag = False
    whois_shielded = False
    
    if not domain.startswith(('http://', 'https://')):
        url = f'https://{domain}'
    else:
        url = domain
        
    ext = tldextract.extract(url)
    root_domain = f"{ext.domain}.{ext.suffix}"
    
    # TLD
    if ext.suffix == 'in' or ext.suffix.endswith('.in'):
        confidence += 30
        evidence.append("TLD is .in")

    # WHOIS Registrant Country
    whois_country = None
    try:
        w = whois.whois(root_domain)
        whois_text = str(w).lower()
        if any(shield in whois_text for shield in PRIVACY_SHIELDS):
            whois_shielded = True
            evidence.append("WHOIS is privacy-shielded (No country conflict check)")
            
        country = w.country
        if isinstance(country, list):
            country = country[0] if country else None
            
        if country:
            whois_country = str(country).upper()
            if whois_country == 'IN' or 'INDIA' in whois_country:
                confidence += 15
                evidence.append("WHOIS country is India (supporting signal)")
            elif not whois_shielded:
                evidence.append(f"WHOIS country is {whois_country}")
    except Exception as e:
        evidence.append("WHOIS lookup failed")

    # 1. Score the Homepage HTML
    if page_html:
        confidence += score_html_content(page_html, "Homepage", evidence)
        
        # 2. Extract and score the Footer specifically
        soup = BeautifulSoup(page_html, 'html.parser')
        footer = soup.find('footer')
        if footer:
            confidence += score_html_content(str(footer), "Homepage Footer", evidence)
            
    # 3. Fetch up to 3 additional relevant subpages if confidence isn't fully maxed
    if confidence < 100:
        target_paths = [
            '/pages/contact', '/pages/contact-us', '/pages/about', 
            '/pages/about-us', '/pages/shipping-policy', '/pages/refund-policy'
        ]
        
        prioritized_urls = []
        if page_html:
            for a in soup.find_all('a', href=True):
                href = a['href'].lower()
                if any(p in href for p in target_paths):
                    full_url = urljoin(url, a['href'])
                    if full_url not in prioritized_urls:
                        prioritized_urls.append(full_url)
                        
        for p in target_paths:
            full_url = urljoin(url, p)
            if full_url not in prioritized_urls:
                prioritized_urls.append(full_url)
                
        pages_checked = 0
        for sub_url in prioritized_urls:
            if pages_checked >= 3 or confidence >= 100:
                break
                
            sub_html = smart_fetch(sub_url)
            if sub_html:
                path_name = urlparse(sub_url).path
                confidence += score_html_content(sub_html, f"Subpage {path_name}", evidence)
            pages_checked += 1

    # Conflict logic
    if confidence >= 30 and whois_country and whois_country not in ['IN', 'INDIA', 'None'] and not whois_shielded:
        conflict_flag = True
        evidence.append(f"CONFLICT: Conflicting WHOIS ({whois_country}) vs page contents/TLD")
        
    confidence = min(100, confidence)
    
    if evidence:
        logger.info(f"India signals for {domain}: Confidence {confidence}%. Shielded: {whois_shielded}. Conflict: {conflict_flag}. Evidence: {', '.join(evidence)}")
    
    return {
        'confidence': confidence,
        'evidence': evidence,
        'conflict_flag': conflict_flag,
        'whois_shielded': whois_shielded
    }
