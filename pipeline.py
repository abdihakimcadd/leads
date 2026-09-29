"""
DROP-IN REPLACEMENT for the block in pipeline.py that starts at
    EMAIL_RE = re.compile(...)
and ends at the last line of find_email_for_site().

Same function names, same args, same return types.
with_retry, save_lead, email_finder_node, verifier, etc. stay untouched.

Add these imports at the top of pipeline.py:
    import html as htmllib
    import urllib3
    from urllib.parse import urljoin, urlparse, unquote
"""

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
MAX_PAGES_NO_SITEMAP = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

JUNK_DOMAINS = {
    "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "example.com",
    "domain.com", "yourdomain.com", "email.com", "mysite.com", "test.com",
}
JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".woff", ".woff2")
JUNK_LOCALS = {"example", "name", "yourname", "youremail", "email", "user", "username", "you"}
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "wa.me", "linktr.ee",
)
PRIORITY_WORDS = (
    "contact", "kontakt", "contatti", "contacto", "impressum", "about", "chi-siamo",
    "nous-contacter", "contactez", "get-in-touch", "reach-us", "team", "support", "enquir", "inquir",
)
GUESS_PATHS = [
    "/contact", "/contact-us", "/contacts", "/about", "/about-us",
    "/impressum", "/kontakt", "/contatti", "/contactez-nous", "/nous-contacter",
]
GENERIC_LOCALS = (
    "info", "contact", "hello", "office", "sales", "enquiries",
    "enquiry", "admin", "support", "mail", "reservations", "booking",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def fetch(url: str) -> Optional[str]:
    """GET with a browser User-Agent. Retries once without SSL verify. None on failure / 4xx / 5xx."""
    for verify in (True, False):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=8, verify=verify)
            return resp.text if resp.status_code < 400 else None
        except requests.exceptions.SSLError:
            continue
        except requests.RequestException:
            return None
    return None


def site_root(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def decode_cfemail(enc: str) -> str:
    """Decode Cloudflare's data-cfemail / email-protection hex string."""
    try:
        key = int(enc[:2], 16)
        return "".join(chr(int(enc[i:i + 2], 16) ^ key) for i in range(2, len(enc), 2))
    except ValueError:
        return ""


def deobfuscate(text: str) -> str:
    """&#64; -> @, [at] -> @, (dot) -> ."""
    t = htmllib.unescape(text)
    t = re.sub(r"\s*[\[\(\{]\s*(?:at|@)\s*[\]\)\}]\s*", "@", t, flags=re.I)
    t = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", t, flags=re.I)
    return t


def clean_email(raw: str) -> Optional[str]:
    """Normalize and reject junk like logo@2x.png, sentry ids, placeholders."""
    addr = unquote(raw).strip().strip(".,;:<>()[]\"'").lower()
    addr = re.sub(r"^(u003[ce]|x3[ce])", "", addr)  # JSON-escaped "<" / ">" glued to the address
    if not EMAIL_RE.fullmatch(addr):
        return None
    local, domain = addr.rsplit("@", 1)
    if addr.endswith(JUNK_SUFFIXES) or local in JUNK_LOCALS:
        return None
    if any(domain == d or domain.endswith("." + d) for d in JUNK_DOMAINS):
        return None
    return addr


def emails_from_html(html: str) -> List[str]:
    """All clean emails on a page, best sources first: mailto -> Cloudflare -> regex on deobfuscated HTML."""
    raw: List[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            raw += re.split(r"[;,]", unquote(href[7:]).split("?")[0])
        elif "email-protection#" in href:
            raw.append(decode_cfemail(href.split("#")[-1]))
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        raw.append(decode_cfemail(tag["data-cfemail"]))
    raw += EMAIL_RE.findall(deobfuscate(html))

    out: List[str] = []
    for r in raw:
        e = clean_email(r)
        if e and e not in out:
            out.append(e)
    return out


def same_site(email: str, host: str) -> bool:
    d = email.split("@")[1]
    return d == host or d.endswith("." + host) or host.endswith("." + d)


def pick_best(emails: List[str], host: Optional[str] = None) -> Optional[str]:
    """Prefer an email on the business's own domain, then generic ones (info@, contact@...)."""
    if not emails:
        return None
    pool = [e for e in emails if host and same_site(e, host)] or emails
    for g in GENERIC_LOCALS:
        for e in pool:
            if e.split("@")[0] == g:
                return e
    return pool[0]


def internal_links(html: str, page_url: str, host: str) -> List[str]:
    """Same-site links, relative ones included."""
    out: List[str] = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(page_url, href).split("#")[0]
        if full.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".zip", ".webp")):
            continue
        if urlparse(full).netloc.lower().removeprefix("www.") == host and full not in out:
            out.append(full)
    return out


def email_snippets(text: str) -> str:
    """Only the parts of the page text around 'email' / '@' — what the LLM actually needs."""
    spans = [text[max(0, m.start() - 120): m.end() + 120] for m in re.finditer(r"(?i)e-?mail|@", text)]
    return " ... ".join(spans)[:4000]


# ---------------------------------------------------------------------------
# same names / signatures as before
# ---------------------------------------------------------------------------

def get_pages_from_sitemap(base_url: str) -> List[str]:
    root = site_root(base_url)
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        xml = fetch(root + path)
        if not xml or ("<urlset" not in xml and "<sitemapindex" not in xml):
            continue
        locs = [l.text.strip() for l in BeautifulSoup(xml, "xml").find_all("loc")]
        if "<sitemapindex" in xml:  # index -> its <loc>s are child sitemaps, open them
            pages: List[str] = []
            for child in locs[:5]:
                child_xml = fetch(child)
                if child_xml:
                    pages += [l.text.strip() for l in BeautifulSoup(child_xml, "xml").find_all("loc")]
            locs = pages
        if locs:
            return locs[:300]
    return []


def extract_email_from_html(html: str) -> Optional[str]:
    return pick_best(emails_from_html(html))


def llm_extract_email(page_text: str) -> Optional[str]:
    try:
        resp = llm_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{
                "role": "user",
                "content": (
                    "Extract a business contact email from this page text. "
                    "Reply with ONLY the email address, or the word NONE if there isn't one.\n\n"
                    + page_text[:4000]
                ),
            }],
            temperature=0,
            max_tokens=30,
        )
        answer = resp.choices[0].message.content.strip()
        m = EMAIL_RE.search(answer)
        if answer.upper() == "NONE" or not m:
            return None
        addr = clean_email(m.group(0))
        if not addr:
            return None
        # anti-hallucination: the address parts must actually appear in the text we sent
        local, domain = addr.split("@")
        t = page_text.lower()
        if local not in t or domain.split(".")[0] not in t:
            return None
        return addr
    except Exception:
        return None


def find_email_for_site(base_url: str) -> Optional[str]:
    root = site_root(base_url)
    host = urlparse(root).netloc.lower().removeprefix("www.")
    if any(host == s or host.endswith("." + s) for s in SOCIAL_HOSTS):
        return None  # Facebook / Instagram "website" — nothing to scrape

    visited: set = set()
    found: List[str] = []
    page_texts: List[str] = []

    def scan(url: str) -> Optional[str]:
        if url in visited:
            return None
        visited.add(url)
        html = fetch(url)
        if not html:
            return None
        for e in emails_from_html(html):
            if e not in found:
                found.append(e)
        page_texts.append(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
        return html

    def done() -> bool:
        return any(same_site(e, host) for e in found)

    home = scan(base_url)
    if home is None and root != base_url:
        home = scan(root)
    if home is None:
        return None  # site down / blocked — don't burn time guessing pages
    if done():
        return pick_best(found, host)

    links = internal_links(home, base_url, host)
    is_priority = lambda u: any(w in u.lower() for w in PRIORITY_WORDS)
    guesses = [root + p for p in GUESS_PATHS]

    stages = [
        lambda: [u for u in links if is_priority(u)] + guesses,           # contact/about pages first
        lambda: [u for u in get_pages_from_sitemap(root) if is_priority(u)],
        lambda: [u for u in links if not is_priority(u)],                  # everything else
    ]

    fetched = 0
    for stage in stages:
        for url in stage():
            if fetched >= MAX_PAGES_NO_SITEMAP:
                break
            if url in visited:
                continue
            scan(url)
            fetched += 1
            if done():
                return pick_best(found, host)

    if found:
        return pick_best(found, host)
    if page_texts:
        return llm_extract_email(email_snippets(" ".join(page_texts)))
    return None
