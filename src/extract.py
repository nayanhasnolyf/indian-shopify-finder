"""
Pull the 7 data fields
"""
import re
import logging
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from .http_utils import default_client, RobotsBlockedError
from .india_detect import smart_fetch

logger = logging.getLogger(__name__)

INDIAN_STATES = [
    'maharashtra', 'karnataka', 'tamil nadu', 'delhi', 'gujarat', 
    'uttar pradesh', 'kerala', 'telangana', 'west bengal', 'haryana', 'punjab'
]

def extract_store_data(domain: str, page_html: str = None) -> dict:
    if not domain.startswith(('http://', 'https://')):
        base_url = f'https://{domain}'
    else:
        base_url = domain
        
    data = {
        'domain_url': base_url,
        'emails': None, 'phones': None, 'socials': None, 'category': None,
        'tagline': None, 'logo_url': None, 'state': None,
        'emails_found': False, 'phones_found': False, 'socials_found': False,
        'category_found': False, 'tagline_found': False, 'logo_url_found': False,
        'state_found': False
    }

    if not page_html:
        page_html = smart_fetch(base_url)

    if not page_html:
        return data

    soup = BeautifulSoup(page_html, 'html.parser')
    
    # 1. Fetch subpages for better extraction
    target_paths = ['/pages/contact', '/pages/contact-us', '/pages/about', '/pages/about-us']
    prioritized_urls = []
    
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        if any(p in href for p in target_paths):
            full_url = urljoin(base_url, a['href'])
            if full_url not in prioritized_urls:
                prioritized_urls.append(full_url)
                
    for p in target_paths:
        full_url = urljoin(base_url, p)
        if full_url not in prioritized_urls:
            prioritized_urls.append(full_url)
            
    subpages_html = []
    pages_checked = 0
    for sub_url in prioritized_urls:
        if pages_checked >= 3:
            break
        sub_html = smart_fetch(sub_url)
        if sub_html:
            subpages_html.append(sub_html)
        pages_checked += 1

    # Combine all pages for deep searching
    all_soups = [soup] + [BeautifulSoup(h, 'html.parser') for h in subpages_html]
    combined_text = " ".join([s.get_text(separator=' ', strip=True) for s in all_soups])
    text_lower = combined_text.lower()

    # 1. Emails
    emails = list(set(re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', combined_text)))
    emails = [e for e in emails if not e.endswith(('sentry.io', 'shopify.com', 'example.com', 'w3.org'))]
    if emails:
        data['emails'] = emails; data['emails_found'] = True

    # 2. Phones
    phone_pattern = r'(?:(?:\+|00)?91)[-\s\.\(\)\[\]]*[6-9](?:[-\s\.\(\)\[\]]*\d){9}'
    raw_phones = re.findall(phone_pattern, combined_text)
    
    toll_free_pattern = r'\b(?:1[-\s\.]?)?800(?:[-\s\.]*\d){6,7}\b'
    raw_tf = re.findall(toll_free_pattern, combined_text)
    
    normalized_phones = []
    for rp in raw_phones:
        digits = re.sub(r'\D', '', rp)
        if digits.startswith('0091') and len(digits) == 14:
            digits = digits[2:]
        if digits.startswith('91') and len(digits) == 12:
            normalized_phones.append('+' + digits)
            
    for tf in raw_tf:
        digits = re.sub(r'\D', '', tf)
        if digits.startswith('800'):
            digits = '1' + digits
            
        if digits.startswith('1800') and len(digits) in [10, 11]:
            # Normalize to 1800-XXX-XXXX format
            if len(digits) == 11:
                normalized_phones.append(f"{digits[:4]}-{digits[4:7]}-{digits[7:]}")
            else:
                normalized_phones.append(f"{digits[:4]}-{digits[4:7]}-{digits[7:]}")
            
    phones = list(set(normalized_phones))
    if phones:
        data['phones'] = phones; data['phones_found'] = True

    # 3. Socials
    socials = {}
    for s in all_soups:
        for a in s.find_all('a', href=True):
            href = a['href'].lower()
            for p, domain_str in {'instagram': 'instagram.com', 'facebook': 'facebook.com', 'twitter': 'twitter.com', 'x': 'x.com', 'linkedin': 'linkedin.com', 'youtube': 'youtube.com'}.items():
                if domain_str in href and p not in socials: socials[p] = a['href']
    if socials:
        data['socials'] = socials; data['socials_found'] = True

    # 4. Category
    categories_to_check = [
        'clothing', 'apparel', 'jewelry', 'electronics', 'beauty', 
        'health', 'home', 'decor', 'furniture', 'skincare', 'cosmetics', 'wellness'
    ]
    category = None
    for s in all_soups:
        if category: break
        meta_desc = s.find('meta', attrs={'name': 'description'})
        og_type = s.find('meta', attrs={'property': 'og:type'})
        
        if meta_desc and meta_desc.get('content'):
            desc = meta_desc.get('content').lower()
            for cat in categories_to_check:
                if cat in desc:
                    category = cat; break
                    
        if not category and og_type and og_type.get('content') not in ['website', 'article']:
            category = og_type.get('content')
            
    if not category:
        # Deep body scan as fallback
        for cat in categories_to_check:
            if cat in text_lower:
                category = cat; break

    if category:
        data['category'] = category; data['category_found'] = True

    # 5. Tagline (Homepage only for clarity)
    tagline = None
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        tagline = meta_desc.get('content').strip()
    else:
        hero = soup.find(['h1', 'h2'])
        if hero: tagline = hero.get_text(strip=True)
    if tagline:
        data['tagline'] = tagline; data['tagline_found'] = True

    # 6. Logo URL (Homepage only)
    logo_url = None
    for img in soup.find_all('img'):
        if img.find_parent(['header', 'nav', 'a']):
            if 'logo' in (" ".join(img.get('class', [])) + " " + img.get('id', '') + " " + img.get('alt', '')).lower():
                src = img.get('src') or img.get('data-src')
                if src and 'favicon' not in src.lower():
                    logo_url = urljoin(base_url, src); break
    if not logo_url:
        match = re.search(r'"logo"\s*:\s*"([^"]+)"', page_html)
        if match:
            logo_src = match.group(1).replace('\\/', '/')
            if 'favicon' not in logo_src.lower(): logo_url = urljoin(base_url, logo_src)
    if logo_url:
        data['logo_url'] = logo_url; data['logo_url_found'] = True

    # 7. State
    found_states = [st for st in INDIAN_STATES if st in text_lower]
    if found_states:
        data['state'] = found_states[0]; data['state_found'] = True

    return data
