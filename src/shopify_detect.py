"""
Confirm a domain is a Shopify store
"""
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from .http_utils import default_client, RobotsBlockedError

logger = logging.getLogger(__name__)

def is_shopify_store(domain: str, page_html: str = None) -> dict:
    if not domain.startswith(('http://', 'https://')):
        url = f'https://{domain}'
    else:
        url = domain

    confidence = 0
    signals = []
    headers_lower = {}

    if not page_html:
        try:
            response = default_client.get(url)
            if not response:
                 return {'confidence': 0, 'signals': ["Failed to fetch url"]}
            if response.status_code != 200:
                return {'confidence': 0, 'signals': [f"Status {response.status_code}"]}
            page_html = response.text
            headers_lower = {k.lower(): v.lower() for k, v in response.headers.items()}
        except RobotsBlockedError:
            return {'confidence': 0, 'signals': ["Blocked by robots.txt"]}
        except Exception as e:
            return {'confidence': 0, 'signals': [f"Error: {e}"]}

    if not page_html:
        return {'confidence': 0, 'signals': ["No HTML provided"]}

    # 1. HTML source patterns
    if 'cdn.shopify.com' in page_html or 'myshopify.com' in page_html:
        confidence += 50
        signals.append("cdn.shopify.com or myshopify.com found in HTML")

    # 3. Headers (only available if live fetch)
    if headers_lower:
        if 'x-shopid' in headers_lower:
            confidence += 30
            signals.append("X-ShopId header present")
        if 'shopify' in headers_lower.get('x-powered-by', '').lower() or 'x-shopify-stage' in headers_lower:
            confidence += 30
            signals.append("Shopify header footprint present")

    # 3. Meta tag
    soup = BeautifulSoup(page_html, 'html.parser')
    if soup.find('meta', attrs={'name': 'shopify-checkout-api-token'}):
        confidence += 30
        signals.append("Shopify checkout API token meta tag found")

    # 4. JS object
    if 'Shopify.theme' in page_html or 'var Shopify =' in page_html:
        confidence += 20
        signals.append("Shopify.theme or Shopify JS object found")

    # 2. Endpoints (Skip if using WARC to avoid live hitting, or run if confident < 100)
    # We will ONLY do these live checks if page_html wasn't passed initially, OR if we really need to.
    # To respect the archival request, we won't blast cart.js unless we fetched live.
    
    confidence = min(100, confidence)
    return {'confidence': confidence, 'signals': signals}
