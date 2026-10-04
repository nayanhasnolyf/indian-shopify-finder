"""
Deduplication logic
"""
import difflib
from urllib.parse import urlparse

def normalize_domain(url: str) -> str:
    """Strips www., http/https, and trailing slashes to get a normalized root domain."""
    if not url:
        return ""
    if not url.startswith(('http://', 'https://')):
        url = "http://" + url
    
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith('www.'):
        domain = domain[4:]
        
    return domain.strip('/')

def is_similar(str1: str, str2: str, threshold: float = 0.85) -> bool:
    """Returns True if the similarity ratio between two strings is above the threshold."""
    if not str1 or not str2:
        return False
    sm = difflib.SequenceMatcher(None, str1.lower().strip(), str2.lower().strip())
    # Cheap upper bounds first: keeps O(n^2) pairwise dedup fast at 1,000+ records
    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
        return False
    return sm.ratio() >= threshold

def deduplicate_data(data_records: list) -> list:
    """
    Deduplicate a list of dictionaries based on:
    1. Normalized root domain
    2. Similarity of logo URL or tagline text (catch same brand on multiple domains)
    """
    if not data_records:
        return []
        
    deduped = []
    seen_domains = set()
    
    for record in data_records:
        url = record.get('domain_url')
        if not url:
            continue
            
        norm_domain = normalize_domain(url)
        
        # 1. Dedupe by normalized domain
        if norm_domain in seen_domains:
            continue
            
        # 2. Dedupe by similarity (brand footprint: tagline or logo)
        tagline = record.get('tagline')
        logo_url = record.get('logo_url')
        
        is_duplicate = False
        for existing in deduped:
            # Check identical logo usage
            ex_logo = existing.get('logo_url')
            if logo_url and ex_logo and logo_url == ex_logo:
                is_duplicate = True
                break
                
            # Check tagline text similarity (only if sufficiently long to avoid false positives like "Home")
            ex_tagline = existing.get('tagline')
            if tagline and ex_tagline and len(tagline) > 15 and len(ex_tagline) > 15:
                if is_similar(tagline, ex_tagline):
                    is_duplicate = True
                    break
                    
        if is_duplicate:
            continue
            
        seen_domains.add(norm_domain)
        deduped.append(record)
        
    return deduped
