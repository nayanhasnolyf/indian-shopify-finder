"""
Authoritative Shopify store metadata via the public /meta.json endpoint, plus
cheap DNS helpers used to pre-filter candidates before any HTTP work.

Every Shopify storefront exposes /meta.json, which includes the shop's own
configured country, currency and province, e.g.
    {"name": "Mamaearth", "country": "IN", "currency": "INR", "province": "Haryana", ...}
This is far more reliable than scraping page text for currency symbols, which
multi-currency widgets inject on non-Indian stores.
"""
import socket
import logging

from .http_utils import default_client, RobotsBlockedError

logger = logging.getLogger(__name__)

# Shopify storefront edge IP range (A records for custom domains on Shopify)
SHOPIFY_IP_PREFIX = '23.227.38.'


def fetch_shopify_meta(domain: str) -> dict | None:
    """Returns the parsed /meta.json dict, or None if unavailable / not Shopify."""
    url = f"https://{domain}/meta.json"
    try:
        resp = default_client.get(url, timeout=12)
    except RobotsBlockedError:
        raise
    except Exception:
        return None
    if not resp or resp.status_code != 200:
        return None
    if 'json' not in resp.headers.get('content-type', '').lower():
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    # Sanity check: real Shopify meta always carries these keys
    if not isinstance(data, dict) or not (data.get('myshopify_domain') or data.get('currency')):
        return None
    return data


def meta_is_india(meta: dict) -> bool | None:
    """True/False if meta states a country, None if it doesn't."""
    country = (meta or {}).get('country')
    if not country:
        return None
    return str(country).upper() in ('IN', 'INDIA')


def resolve_ip(domain: str) -> str | None:
    """DNS A-record lookup via the OS resolver. Returns None for unresolvable hosts."""
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return None


def resolves(domain: str) -> bool:
    """True if the domain (or its www. variant) resolves."""
    if resolve_ip(domain):
        return True
    if not domain.startswith('www.'):
        return resolve_ip(f'www.{domain}') is not None
    return False


def is_shopify_dns(domain: str) -> bool:
    """True if the apex or www host points at Shopify's storefront IPs."""
    hosts = [domain] if domain.startswith('www.') else [domain, f'www.{domain}']
    for h in hosts:
        ip = resolve_ip(h)
        if ip and ip.startswith(SHOPIFY_IP_PREFIX):
            return True
    return False
