"""
HTTP Client utilities including rate limiting and robots.txt parsing.
"""
import time
import threading
import urllib.robotparser
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter

class RobotsBlockedError(Exception):
    """Raised when robots.txt disallows access to a URL."""
    pass

class RateLimitedClient:
    def __init__(self, delay=1.0, pool_size=64):
        self.delay = delay
        self.last_request_time = {}
        self.robot_parsers = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        # Bounded connection pool: prevents Windows socket exhaustion (WinError 10053/10054)
        # when many threads hit many hosts concurrently.
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        self.session.headers.update({
            'User-Agent': 'IndianShopifyFinderBot/1.0 (contact: you@example.com)'
        })

    def _get_robot_parser(self, domain_url):
        with self._lock:
            if domain_url in self.robot_parsers:
                return self.robot_parsers[domain_url]

        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{domain_url}/robots.txt")
        # NOTE: rp.read() uses urllib with the default "Python-urllib" User-Agent, which
        # Shopify/Cloudflare frequently 403. The stdlib then sets disallow_all=True, and
        # on network errors last_checked stays 0 so can_fetch() always returns False.
        # Fetch it ourselves and follow the common convention (also used by Google):
        # unreachable or 4xx robots.txt => no restrictions.
        try:
            resp = self.session.get(f"{domain_url}/robots.txt", timeout=10)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.allow_all = True
        except Exception:
            rp.allow_all = True
        rp.modified()  # marks as checked so can_fetch() evaluates the rules

        with self._lock:
            self.robot_parsers[domain_url] = rp
        return rp

    def get(self, url, headers=None, ignore_robots=False, timeout=10):
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        
        # Check robots.txt
        if not ignore_robots:
            rp = self._get_robot_parser(domain)
            if not rp.can_fetch(self.session.headers['User-Agent'], url):
                raise RobotsBlockedError(f"Robots.txt disallowed fetching {url}")
            
        # Rate Limiter (per host)
        with self._lock:
            now = time.time()
            last_time = self.last_request_time.get(domain, 0)
            wait = max(0.0, self.delay - (now - last_time))
            # Reserve the slot so concurrent threads to the same host queue up correctly
            self.last_request_time[domain] = now + wait
        if wait > 0:
            time.sleep(wait)
            
        req_headers = self.session.headers.copy()
        if headers:
            req_headers.update(headers)
        response = self.session.get(url, headers=req_headers, timeout=timeout)
        with self._lock:
            self.last_request_time[domain] = time.time()
        return response

# Configurable default client
default_client = RateLimitedClient(delay=2.0)
