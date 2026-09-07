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
from typing import TypedDict, Optional, List

import requests
import dns.resolver
from bs4 import BeautifulSoup
from openai import OpenAI
from apify_client import ApifyClient
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()  # only matters for local runs — Streamlit Cloud uses st.secrets instead


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
    max_results: int
    leads: List[Lead]
    status: str


# ---------------------------------------------------------------------------
# AGENT 1 — COLLECTOR
# ---------------------------------------------------------------------------

def collector_node(state: PipelineState) -> PipelineState:
    run = apify_client().actor("compass/crawler-google-places").call(run_input={
        "searchStringsArray": [state["business_type"]],
        "locationQuery": state["country"],
        "maxCrawledPlacesPerSearch": state["max_results"],  # correct param name — caps Apify usage per run
    })
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


def get_pages_from_sitemap(base_url: str) -> List[str]:
    for path in ["/sitemap.xml", "/sitemap_index.xml"]:
        try:
            resp = requests.get(base_url.rstrip("/") + path, timeout=8)
            if resp.status_code == 200 and ("<urlset" in resp.text or "<sitemapindex" in resp.text):
                soup = BeautifulSoup(resp.text, "xml")
                urls = [loc.text for loc in soup.find_all("loc")]
                if urls:
                    return urls[:30]
        except requests.RequestException:
            continue
    return []


def extract_email_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("mailto:"):
            addr = a["href"].replace("mailto:", "").split("?")[0].strip()
            if EMAIL_RE.match(addr):
                return addr
    match = EMAIL_RE.search(html)
    return match.group(0) if match else None


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
        if answer.upper() == "NONE" or "@" not in answer:
            return None
        return answer
    except Exception:
        return None


def find_email_for_site(base_url: str) -> Optional[str]:
    pages = get_pages_from_sitemap(base_url)

    if not pages:
        pages = [base_url]
        try:
            resp = requests.get(base_url, timeout=8)
            soup = BeautifulSoup(resp.text, "html.parser")
            links = [a["href"] for a in soup.find_all("a", href=True)]
            same_domain = [l for l in links if base_url.split("//")[-1].split("/")[0] in l]
            pages.extend(same_domain[:MAX_PAGES_NO_SITEMAP])
        except requests.RequestException:
            pass

    page_texts = []
    for url in pages[:MAX_PAGES_NO_SITEMAP]:
        try:
            resp = requests.get(url, timeout=8)
            email = extract_email_from_html(resp.text)
            if email:
                return email
            page_texts.append(BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True))
        except requests.RequestException:
            continue

    if page_texts:
        return llm_extract_email(" ".join(page_texts)[:6000])
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


def run_pipeline(business_type: str, country: str, max_results: int = 50) -> PipelineState:
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
