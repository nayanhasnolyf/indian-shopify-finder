"""
HTTP Client utilities including rate limiting and robots.txt parsing.
"""
import time
import urllib.robotparser
from urllib.parse import urlparse
import requests

class RobotsBlockedError(Exception):
    """Raised when robots.txt disallows access to a URL."""
    pass

class RateLimitedClient:
    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_request_time = {}
        self.robot_parsers = {}
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'IndianShopifyFinderBot/1.0 (contact: you@example.com)'
        })

    def _get_robot_parser(self, domain_url):
        if domain_url not in self.robot_parsers:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{domain_url}/robots.txt")
            try:
                rp.read()
            except Exception:
                pass
            self.robot_parsers[domain_url] = rp
        return self.robot_parsers[domain_url]

    def get(self, url, headers=None, ignore_robots=False, timeout=10):
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        
        # Check robots.txt
        if not ignore_robots:
            rp = self._get_robot_parser(domain)
            if not rp.can_fetch(self.session.headers['User-Agent'], url):
                raise RobotsBlockedError(f"Robots.txt disallowed fetching {url}")
            
        # Rate Limiter
        now = time.time()
        last_time = self.last_request_time.get(domain, 0)
        elapsed = now - last_time
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
            
        try:
            req_headers = self.session.headers.copy()
            if headers:
                req_headers.update(headers)
            response = self.session.get(url, headers=req_headers, timeout=timeout)
            self.last_request_time[domain] = time.time()
            return response
        except requests.RequestException as e:
            raise

# Configurable default client
default_client = RateLimitedClient(delay=2.0)
