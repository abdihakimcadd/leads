"""
pipeline.py — the 3-agent outreach lead pipeline (collector -> email_finder -> verifier)

This file has NO Streamlit code in it — it's pure pipeline logic, imported by app.py.
Keeping them separate means you can also run this from a plain terminal/cron job later.
"""

import os
import re
import time
import socket
import smtplib
import uuid
import html as htmllib
from typing import TypedDict, Optional, List
from urllib.parse import urljoin, urlparse, unquote

import requests
import urllib3
import dns.resolver
from bs4 import BeautifulSoup
from openai import OpenAI
from apify_client import ApifyClient
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()  # only matters for local runs — Streamlit Cloud uses st.secrets instead

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------------------------------------------------------------------------
# SECRETS — works both locally (.env) and on Streamlit Cloud (st.secrets)
# ---------------------------------------------------------------------------

def get_secret(key: str) -> str:
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass  # not running inside Streamlit, or secrets not set — fall back below

    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Missing secret: {key} — set it in .env locally or Streamlit Secrets.")
    return value


# ---------------------------------------------------------------------------
# CLIENTS — created lazily so importing this file never fails before secrets exist
# ---------------------------------------------------------------------------

_llm_client = None
_apify_client = None
_supabase_client = None


def llm_client():
    global _llm_client
    if _llm_client is None:
        _llm_client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=get_secret("GROQ_API_KEY"))
    return _llm_client


def apify_client():
    global _apify_client
    if _apify_client is None:
        _apify_client = ApifyClient(get_secret("APIFY_TOKEN"))
    return _apify_client


def supabase_client():
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(get_secret("SUPABASE_URL"), get_secret("SUPABASE_KEY"))
    return _supabase_client


LLM_MODEL = "llama-3.1-8b-instant"


# ---------------------------------------------------------------------------
# STATE
# ---------------------------------------------------------------------------

class Lead(TypedDict):
    name: str
    website: str
    country: str
    business_type: str
    email: Optional[str]
    verified: Optional[str]     # "valid" | "invalid" | "risky" | "unknown"
    error: Optional[str]


class PipelineState(TypedDict):
    run_id: str
    business_type: str
    country: str
    max_results: Optional[int]
    leads: List[Lead]
    status: str


# ---------------------------------------------------------------------------
# AGENT 1 — COLLECTOR
# ---------------------------------------------------------------------------

def collector_node(state: PipelineState) -> PipelineState:
    run_input = {
        "searchStringsArray": [state["business_type"]],
        "locationQuery": state["country"],
    }
    if state["max_results"]:  # only add the cap if one was actually given — None/0 means no limit
        run_input["maxCrawledPlacesPerSearch"] = state["max_results"]

    run = apify_client().actor("compass/crawler-google-places").call(run_input=run_input)
    items = apify_client().dataset(run.default_dataset_id).list_items().items

    leads: List[Lead] = []
    for item in items:
        website = item.get("website")
        if not website:
            continue
        leads.append({
            "name": item.get("title", ""),
            "website": website,
            "country": state["country"],
            "business_type": state["business_type"],
            "email": None,
            "verified": None,
            "error": None,
        })

    state["leads"] = leads
    state["status"] = "extracting"
    return state


# ---------------------------------------------------------------------------
# AGENT 2 — EMAIL FINDER
# ---------------------------------------------------------------------------

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


def with_retry(fn, *args, retries=2, delay=2, **kwargs):
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(delay)


def save_lead(state: PipelineState, lead: Lead):
    """Upsert one lead row into Supabase — called immediately after each lead
    is processed, in both agent 2 and agent 3, so a crash only loses one row."""
    row = {**lead, "run_id": state["run_id"]}
    supabase_client().table("leads").upsert(row, on_conflict="run_id,website").execute()


def email_finder_node(state: PipelineState) -> PipelineState:
    for lead in state["leads"]:
        try:
            lead["email"] = with_retry(find_email_for_site, lead["website"])
        except Exception as e:
            lead["error"] = str(e)
        save_lead(state, lead)

    state["status"] = "verifying"
    return state


# ---------------------------------------------------------------------------
# AGENT 3 — VERIFIER  (checks the mail server, sends nothing)
# ---------------------------------------------------------------------------

def get_mx_host(domain: str) -> Optional[str]:
    try:
        answers = dns.resolver.resolve(domain, "MX")
        best = min(answers, key=lambda r: r.preference)
        return str(best.exchange).rstrip(".")
    except Exception:
        return None


def verify_email(email: str) -> str:
    domain = email.split("@")[-1]
    mx_host = get_mx_host(domain)
    if not mx_host:
        return "invalid"

    fake_check_address = f"nonexistent-check-{int(time.time())}@{domain}"

    try:
        smtp = smtplib.SMTP(timeout=8)
        smtp.connect(mx_host, 25)
        smtp.helo(get_secret("SENDING_DOMAIN"))
        smtp.mail(get_secret("SENDING_ADDRESS"))

        code_real, _ = smtp.rcpt(email)
        code_fake, _ = smtp.rcpt(fake_check_address)
        smtp.quit()

        if code_fake == 250:
            return "risky"
        if code_real == 250:
            return "valid"
        return "invalid"

    except (socket.timeout, ConnectionRefusedError):
        return "unknown"   # port 25 is blocked on most cloud hosts, incl. Streamlit Cloud — see note in app.py
    except Exception:
        return "unknown"


def verifier_node(state: PipelineState) -> PipelineState:
    for lead in state["leads"]:
        if not lead["email"]:
            continue
        try:
            lead["verified"] = with_retry(verify_email, lead["email"], retries=1, delay=3)
        except Exception as e:
            lead["error"] = str(e)
            lead["verified"] = "unknown"
        save_lead(state, lead)

    state["status"] = "done"
    return state


# ---------------------------------------------------------------------------
# ORCHESTRATOR
# ---------------------------------------------------------------------------

from langgraph.graph import StateGraph, END


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("collector", collector_node)
    graph.add_node("email_finder", email_finder_node)
    graph.add_node("verifier", verifier_node)
    graph.set_entry_point("collector")
    graph.add_edge("collector", "email_finder")
    graph.add_edge("email_finder", "verifier")
    graph.add_edge("verifier", END)
    return graph.compile()


def run_pipeline(business_type: str, country: str, max_results: Optional[int] = None) -> PipelineState:
    """Single entry point app.py calls — runs all 3 agents, returns final state."""
    app = build_graph()
    initial_state: PipelineState = {
        "run_id": str(uuid.uuid4()),
        "business_type": business_type,
        "country": country,
        "max_results": max_results,
        "leads": [],
        "status": "collecting",
    }
    return app.invoke(initial_state, config={"configurable": {"thread_id": initial_state["run_id"]}})
