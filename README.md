# Indian Shopify Finder

A pipeline to discover and extract data from Indian Shopify stores.

## Final Output & Runtime
- **Final Store Count**: 303 premium, high-confidence Indian D2C Shopify brands.
- **Goal vs. Achieved**: The original goal was 1,000+ stores. We achieved 303.
- **Reasoning for Gap**: Generating 1,000+ true Indian stores from global data sets requires massive brute-force computing. The `*.myshopify.com` wildcard query yields about 0.02 Indian stores per page of the Common Crawl index. To hit 1,000 stores, the script would need to query 50,000 pages of the Common Crawl API, which due to aggressive rate limiting (requiring 30-60s backoffs) takes roughly **30+ hours** just for URL sourcing, and significantly more time to fetch and verify the hundreds of thousands of resulting domains. Instead, we pivoted to a high-precision, low-volume approach targeting curated static lists and dynamic listicles.
- **Runtime**: The final pipeline (sourcing 12,000+ links from listicles, running live detection on 2,213 domains) runs in about **5-6 minutes**.

## Methodology
The final pipeline utilizes a combined approach of custom-domain sourcing rather than blind wildcards:
1. **Seed List**: A small manual seed list of highly established brands (e.g. `mamaearth.in`, `boat-lifestyle.com`).
2. **Static Roundup Scraping**: Dynamically parsing HTML from top D2C blog posts (e.g., "Top 100 Indian D2C Brands") to extract custom domains (`.com`, `.in`, `.co.in`) via regex, bypassing noisy generic subdomains entirely.
3. **Targeted Serper Hits**: Select high-quality stores surfaced via targeted natural language queries.
4. **Live Detection**: Fetching live HTML (including subpages like `/contact`) and passing it through a tiered confidence engine looking for Indian locations, currency symbols, and `+91` phone prefixes.
5. **Extraction**: Using Beautiful Soup and regex to pull emails, phones, socials, categories, and logos.

## Known Limitations & False Positives

1. **`*.myshopify.com` Oversampling**: Searching the Common Crawl for `*.myshopify.com` URLs was abandoned because it heavily oversamples global hobby stores, abandoned drop-shipping sites, and free-tier accounts. Real brands overwhelmingly use custom domains.
2. **The Currency-Widget False Positive (721 Domains)**: Modern Shopify stores utilize dynamic multi-currency converter widgets that inject global currency symbols (including `₹` or `INR`) directly into the DOM for all users globally. This triggers a false positive in the detection engine, elevating the confidence score to 30% ("Borderline") for over 700 non-Indian stores.
3. **Serper's 8% Signal-to-Noise Ratio**: Due to Google's strict blocking of advanced operators (like `site:.in`) on the free Serper API, we fell back to natural language queries ("top indian shopify stores"). This heavily indexes SaaS platforms, app store listings, and blogs rather than the stores themselves, yielding roughly 2 valid stores out of 24 returned domains.
4. **CC-Index TLD Scan Limitation**: The Common Crawl Index API structurally drops the connection (500-level error) when attempting to run a TLD-level wildcard query like `*.in/*`. It requires scoping to a specific subdomain, which prevents us from easily finding all `.in` domains in the index.
5. **Phone Number Miss Rate**: Despite having robust regex capable of extracting `+91`, `091`, and toll-free `1800` numbers, the pipeline still sees an 18.8% miss rate on phone fields (down from 30% after pivoting to premium brands). This is not an extraction failure, but a structural reality of modern D2C brands heavily relying on chat widgets, ticketing systems (Zendesk), or email support over public phone lines.
6. **Socket Exhaustion at Scale**: When attempting to fetch ~2,400 unfiltered, raw domain string candidates extracted via Google Snippets (many of which were dead links or unresolvable DNS records), the naive multi-threaded `requests` pool encountered catastrophic Windows socket exhaustion (`WinError 10054` / `WinError 10053`) due to too many concurrent connections hanging on unresolvable hosts. A production version scaling beyond this would require a bounded connection pool (e.g., `requests.Session` with a hard pool size cap, or `asyncio` with a semaphore limiting concurrent connections) along with a fast upfront DNS-resolution filter before attempting full HTTP fetches.

## Structure
- `src/sourcing.py`: candidate domain discovery (static list scraping)
- `src/shopify_detect.py`: confirm a domain is a Shopify store
- `src/india_detect.py`: confirm a store is India-based using a confidence scoring engine
- `src/extract.py`: pull the 7 data fields (emails, phones, etc.)
- `src/dedup.py`: deduplication logic
- `src/pipeline.py`: orchestrates the above, multi-threaded
- `data/`: output CSVs and JSON go here

## Setup
```bash
python -m venv venv
source venv/bin/activate  # Or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
```

## Running the Pipeline
```bash
python -m src.pipeline
```
