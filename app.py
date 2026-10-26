import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import re
import concurrent.futures
import plotly.express as px
import plotly.graph_objects as go
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import datetime
import pytz
from fpdf import FPDF
import io
import matplotlib
matplotlib.use("Agg")   # headless rendering for chart images
import matplotlib.pyplot as plt
import os
import html
import random
import threading
import time
from streamlit_gsheets import GSheetsConnection

# --- CONFIGURATION & CUSTOM CSS ---
st.set_page_config(page_title="Auto-Sales Intelligence Agent", layout="wide")

st.markdown("""
    <style>
        [data-testid="stSidebarHeader"] { position: sticky !important; top: 0 !important; z-index: 999 !important; background-color: var(--secondary-background-color) !important; }
        [data-testid="stSidebarResizer"] { height: 100vh !important; }
        button[kind="primary"] { background-color: #D70015 !important; color: white !important; border: 1px solid #A30010 !important; font-weight: bold !important; border-radius: 6px !important; }
        button[kind="primary"]:hover { background-color: #FF3B30 !important; border: 1px solid #D70015 !important; color: white !important; }
    </style>
""", unsafe_allow_html=True)

# --- SESSION STATE INITIALIZATION ---
if 'history' not in st.session_state:
    st.session_state.history = {} 
if 'current_report_id' not in st.session_state:
    st.session_state.current_report_id = None 
if 'min_visitors' not in st.session_state:
    st.session_state.min_visitors = 1
# High-Engagement threshold (campaign visits) and how it's applied:
#   'highlight' (default) - full report; the T+ visit vehicles are shown as a
#                           complementary subset alongside it
#   'filter'              - the whole report runs on T+ visit vehicles only
#                           (the original VDP filter behaviour)
if 'he_threshold' not in st.session_state:
    st.session_state.he_threshold = 5
if 'vdp_mode' not in st.session_state:
    st.session_state.vdp_mode = 'highlight'
if 'global_usage_count' not in st.session_state:
    st.session_state.global_usage_count = "..."
if 'mc_month_usage' not in st.session_state:
    st.session_state.mc_month_usage = None

# --- THE DEALER API VAULT (Google Sheets Powered) ---
FALLBACK_VAULT = {
    'hananiavw.com': {'app_id': 'YL5AFXM3DW', 'api_key': '59d32b7b5842f84284e044c7ca465498', 'index': 'volkswagenoforangepark-sbm0424_production_inventory'},
    'hondasanmarcos.com': {'app_id': 'V3ZOVI2QFZ', 'api_key': 'ec7553dd56e6d4c8bb447a0240e7aab3', 'index': 'hondaofsanmarcos_production_inventory'}
}

def _clean_cell(value):
    """Normalize a sheet cell to a stripped string ('' for blanks/NaN)."""
    text = str(value).strip()
    return "" if text.lower() in ("", "nan", "none") else text

def _row_is_ignored(app_id, api_key, index_name):
    """A domain is IGNORE if any credential column is literally IGNORE (case-insensitive)."""
    return any(v.upper() == "IGNORE" for v in (app_id, api_key, index_name))

def build_vault_dict(vault_df):
    """
    Turn the sheet DataFrame into the in-memory vault.
    Ignored domains are stored as {'ignore': True} so the scanner has a single,
    foolproof flag to check. Domains with missing creds are simply left out
    (they fall through to the visual scraper).
    """
    vault_dict = {}
    for _, row in vault_df.iterrows():
        domain = _clean_cell(row.get('Base Domain', ''))
        if not domain:
            continue
        app_id = _clean_cell(row.get('App ID', ''))
        api_key = _clean_cell(row.get('API Key', ''))
        index_name = _clean_cell(row.get('Index Name', ''))

        if _row_is_ignored(app_id, api_key, index_name):
            vault_dict[domain] = {'ignore': True}          # hard kill switch
        elif app_id and api_key and index_name:
            vault_dict[domain] = {
                'app_id': app_id,
                'api_key': api_key,
                'index': index_name,
            }
        # else: incomplete row -> not added -> scraper handles it
    return vault_dict

def load_vault():
    """
    The Google Sheet is the ONLY source of truth. Returns:
      (vault_dict, vault_df, conn, loaded_ok)
    On failure we fall back to FALLBACK_VAULT but flag loaded_ok=False so the UI
    can warn the user rather than silently running on stale/tiny credentials.
    """
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        vault_df = conn.read(worksheet="Sheet1", ttl=60)
        vault_dict = build_vault_dict(vault_df)

        try:
            usage_df = conn.read(worksheet="UsageStats", ttl=60)
            st.session_state.global_usage_count = len(usage_df)
            # Sum this month's market lookups from our own report log, so the
            # sidebar can show remaining budget without spending an API call.
            if "MarketCheck Calls" in usage_df.columns:
                ts = pd.to_datetime(usage_df["Timestamp"], errors="coerce", utc=True)
                now_ts = pd.Timestamp.now(tz="UTC")
                in_month = (ts.dt.year == now_ts.year) & (ts.dt.month == now_ts.month)
                st.session_state.mc_month_usage = int(
                    pd.to_numeric(usage_df.loc[in_month, "MarketCheck Calls"],
                                  errors="coerce").fillna(0).sum()
                )
        except Exception:
            pass

        # Client-facing dealer names (tab "DealerNames": Domain, Display Name).
        # Best-effort: a missing tab just means names fall back to the domain.
        try:
            names_df = conn.read(worksheet="DealerNames", ttl=60)
            names, flags = {}, {}
            for _, r in names_df.iterrows():
                dom = str(r.get("Domain", "") or "").strip().lower()
                dom = re.sub(r'^https?://', '', dom).replace("www.", "").split("/")[0]
                disp = str(r.get("Display Name", "") or "").strip()
                if dom and disp and disp.lower() != "nan":
                    names[dom] = disp
                # Optional column: set it only when the automatic call is wrong.
                g = str(r.get("Is Group Site", "") or "").strip().lower()
                if dom and g in ("true", "yes", "y", "1"):
                    flags[dom] = True
                elif dom and g in ("false", "no", "n", "0"):
                    flags[dom] = False
            st.session_state.dealer_display_names = names
            st.session_state.dealer_group_flags = flags
        except Exception:
            st.session_state.setdefault("dealer_display_names", {})

        return vault_dict, vault_df, conn, True
    except Exception:
        return dict(FALLBACK_VAULT), None, None, False

DEALER_API_VAULT, vault_df, gsheets_conn, VAULT_LOADED_OK = load_vault()

if not VAULT_LOADED_OK:
    st.warning(
        "Couldn't load saved dealer settings right now — some dealers may not "
        "resolve, and new dealers can't be saved until the connection is back.",
        icon="⚠️",
    )

def upsert_vault_row(domain, app_id, api_key, index_name):
    """
    Single write path for the vault. Keeps the sheet authoritative:
      1. Re-read the live sheet (ttl=0, no cache)
      2. Drop any existing row(s) for this domain (prevents duplicate/ghost rows)
      3. Append the new row
      4. Write back and bust the cache so the next load re-reads fresh
    Returns True on success. There is NO local session overlay — after this
    returns, the app reruns and reloads straight from the sheet.
    """
    if gsheets_conn is None:
        return False
    try:
        live_df = gsheets_conn.read(worksheet="Sheet1", ttl=0)
        live_df = live_df[live_df['Base Domain'].astype(str).str.strip() != '']
        # Remove existing entries for this domain (case-insensitive) to avoid dupes
        keep_mask = live_df['Base Domain'].astype(str).str.strip().str.lower() != domain.lower()
        live_df = live_df[keep_mask]
        new_row = pd.DataFrame([{
            'Base Domain': domain,
            'App ID': app_id,
            'API Key': api_key,
            'Index Name': index_name,
        }])
        updated_df = pd.concat([live_df, new_row], ignore_index=True)
        gsheets_conn.update(worksheet="Sheet1", data=updated_df)
        st.cache_data.clear()
        return True
    except Exception:
        return False

# --- VAULT ONBOARDING HELPERS (salesperson-friendly) ---
def normalize_domain(raw):
    """Strip protocol / www / paths down to a bare base domain."""
    d = str(raw).strip().lower()
    d = re.sub(r'^https?://', '', d)
    d = d.replace('www.', '')
    d = d.split('/')[0].split('?')[0].strip().strip('/')
    return d

def parse_algolia_credentials(text, fallback_domain=""):
    """
    Forgiving extractor. Accepts ANY of:
      • Bookmarklet output:   domain|APPID|APIKEY|indexname
      • A raw Algolia URL:    ...x-algolia-application-id=APPID&x-algolia-api-key=APIKEY.../indexes/NAME/query
      • Messy pasted config:  loose text containing the three values
    Returns dict(domain, app_id, api_key, index) or None if it can't find all three.
    """
    if not text:
        return None
    text = str(text).strip()
    domain, app_id, api_key, index = fallback_domain, "", "", ""

    # 1) Pipe format from the bookmarklet: domain|appId|apiKey|index
    if text.count('|') >= 3:
        parts = [p.strip() for p in text.split('|')]
        domain = parts[0] or domain
        app_id, api_key, index = parts[1], parts[2], parts[3]

    # 2) URL query-param format (the old DevTools method)
    if not app_id:
        m = re.search(r'x-algolia-application-id=([A-Za-z0-9]+)', text)
        if m: app_id = m.group(1)
    if not api_key:
        m = re.search(r'x-algolia-api-key=([^&\s"\']{20,})', text, re.I)
        if m: api_key = m.group(1)
    if not index:
        m = re.search(r'/indexes/([^/?"\']+)/(?:query|queries)', text)
        if m: index = m.group(1)

    # 3) Loose fallbacks — pull the values out of raw page text / config
    if not app_id:
        m = (re.search(r'([A-Z0-9]{10})-dsn\.algolia\.net', text)
             or re.search(r'(?:application-?id|appid|app_id)["\'\s:=]+([A-Z0-9]{10})', text, re.I))
        if m: app_id = m.group(1).upper()
    if not api_key:
        m = re.search(r'(?:x-algolia-api-key|algolia[_-]?api[_-]?key|api[_-]?key|apikey|searchapikey|search[_-]?key)["\'\s:=]+["\']?([A-Za-z0-9+/=_-]{20,})', text, re.I)
        if m: api_key = m.group(1)
    if not index:
        m = (re.search(r'["\']?index(?:name)?["\']?\s*[:=]\s*["\']([a-z0-9_\-]*inventory[a-z0-9_\-]*)["\']', text, re.I)
             or re.search(r'([a-z0-9][a-z0-9_\-]*_production_inventory[a-z0-9_]*)', text, re.I))
        if m: index = m.group(1)

    if app_id and api_key and index:
        return {
            'domain': normalize_domain(domain) if domain else "",
            'app_id': app_id.strip(),
            'api_key': api_key.strip(),
            'index': index.strip(),
        }
    return None

def autodetect_algolia(domain):
    """
    Server-side attempt: fetch the dealer's pages and scrape the Algolia creds
    out of the page config. An aggressive firewall can block this — in which case
    the salesperson should use the Bookmarklet (runs in their browser).
    Returns a creds dict or None.
    """
    domain = normalize_domain(domain)
    if not domain:
        return None
    candidate_paths = ['', '/new-inventory/', '/used-inventory/', '/inventory/', '/searchnew.aspx']
    blob = ""
    for path in candidate_paths:
        url = f"https://www.{domain}{path}"
        try:
            r = requests.get(url, timeout=8, allow_redirects=True,
                             headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                                                    'Chrome/122.0.0.0 Safari/537.36'})
            if r.status_code == 200 and r.text:
                blob += "\n" + r.text
                creds = parse_algolia_credentials(blob, fallback_domain=domain)
                if creds:  # found everything — stop early
                    return creds
        except Exception:
            continue
    return parse_algolia_credentials(blob, fallback_domain=domain)

def test_algolia_creds(app_id, api_key, index, domain=""):
    """
    Fire ONE Algolia request against the given creds to diagnose them.
    Sends the same Referer/Origin as scan_url so a referer-locked key is tested
    the way it will actually be used. Returns (ok, headline, detail) in plain
    English so a salesperson can see whether the keys work.
    """
    if not (app_id and api_key and index):
        return False, "Missing keys", "One of App ID / API Key / Index is blank."
    endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {
        "x-algolia-application-id": app_id,
        "x-algolia-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    if domain:
        d = normalize_domain(domain)
        headers["Referer"] = f"https://www.{d}/"
        headers["Origin"] = f"https://www.{d}"
    try:
        r = requests.post(endpoint, headers=headers,
                          json={"params": "query=&hitsPerPage=1"}, timeout=8)
    except Exception as e:
        return False, "Couldn't reach Algolia", str(e)[:200]

    if r.status_code == 200:
        try:
            data = r.json()
            n = data.get("nbHits", 0)
        except Exception:
            return True, "Keys work, but the response wasn't readable", "Got a 200 but couldn't parse the JSON."
        if isinstance(n, int) and n == 0:
            return True, "Keys work, but this index looks empty (0 cars)", \
                   "The App ID + Key are valid, but the Index Name may be wrong or the inventory feed is empty."
        return True, f"✅ Keys work — {n} vehicles in this index", \
               "This dealer is ready to resolve via the inventory API."
    if r.status_code in (401, 403):
        return False, "❌ Algolia rejected the key (403)", \
               "The saved key is rejected even with a matching Referer. First, re-grab a fresh key with the 🔑 bookmark in case the saved one is just stale. If a fresh key is ALSO rejected, the dealer's platform has locked it — it can't be reached from here, so mark it Ignore (not scannable)."
    if r.status_code == 404:
        return False, "❌ Index not found (404)", \
               f"The App ID + Key may be fine, but the Index Name '{index}' doesn't exist. Re-check the index."
    return False, f"❌ Algolia returned {r.status_code}", str(r.text)[:200]

# One-click bookmarklet: runs in the salesperson's OWN browser (no firewall can
# block it). It scrapes App ID + Index from the page AND hooks the network layer
# to capture the Search Key from a live Algolia request (many dealers only send
# the key as a request header when a search actually runs — not in the page HTML).
# Flow: click once to arm + scan. If the key isn't in the page, type a letter in
# the dealer's inventory search box, then click the bookmark again to grab it.
BOOKMARKLET_JS = (
    "javascript:(function(){try{"
    "var W=window;"
    "if(!W.__ffArmed){W.__ffArmed=1;"
    "W.__ffKeys={app:'',key:'',idx:'',dom:location.hostname.replace(/^www\\./,'')};"
    "var b=document.documentElement.innerHTML+'\\n'+Array.prototype.map.call(document.scripts,function(s){return s.textContent||''}).join('\\n');"
    "try{b+='\\n'+performance.getEntriesByType('resource').map(function(e){return e.name}).join('\\n')}catch(e){}"
    "function M(r){var x=b.match(r);return x?x[1]:''}"
    "W.__ffKeys.app=M(/([A-Z0-9]{10})-dsn\\.algolia\\.net/)||M(/(?:application-?id|appid|app_id)[\"'\\s:=]+([A-Z0-9]{10})/i);"
    "W.__ffKeys.idx=M(/[\"']?index(?:name)?[\"']?\\s*[:=]\\s*[\"']([a-z0-9_\\-]*inventory[a-z0-9_\\-]*)[\"']/i)||M(/([a-z0-9][a-z0-9_\\-]*_production_inventory[a-z0-9_]*)/i);"
    "var k0=M(/(?:x-algolia-api-key|api-?key|apikey|searchapikey)[\"'\\s:=]+[\"']?([A-Za-z0-9+\\/=_-]{20,})/i);if(k0)W.__ffKeys.key=k0;"
    "if(!W.__ffKeys.key){var ku=b.match(/x-algolia-api-key=([^&\\s\"']+)/i);if(ku)W.__ffKeys.key=decodeURIComponent(ku[1])}"
    "var oO=XMLHttpRequest.prototype.open,oS=XMLHttpRequest.prototype.setRequestHeader;"
    "XMLHttpRequest.prototype.open=function(m,u){try{if(/algolia/i.test(u)){var a=String(u).match(/([A-Z0-9]{10})-dsn/);if(a&&!W.__ffKeys.app)W.__ffKeys.app=a[1];var k=String(u).match(/x-algolia-api-key=([^&]+)/i);if(k)W.__ffKeys.key=decodeURIComponent(k[1])}}catch(e){}return oO.apply(this,arguments)};"
    "XMLHttpRequest.prototype.setRequestHeader=function(h,v){try{var H=String(h).toLowerCase();if(H=='x-algolia-api-key')W.__ffKeys.key=v;if(H=='x-algolia-application-id'&&v)W.__ffKeys.app=v}catch(e){}return oS.apply(this,arguments)};"
    "var oF=W.fetch;if(oF){W.fetch=function(res,opt){try{var u=(res&&res.url)||res;if(/algolia/i.test(String(u))){var a=String(u).match(/([A-Z0-9]{10})-dsn/);if(a&&!W.__ffKeys.app)W.__ffKeys.app=a[1];var k=String(u).match(/x-algolia-api-key=([^&]+)/i);if(k)W.__ffKeys.key=decodeURIComponent(k[1])}if(opt&&opt.headers){var H=opt.headers;var g=function(n){try{return H.get?H.get(n):(H[n]||H[n.toLowerCase()])}catch(e){return''}};var kk=g('x-algolia-api-key');if(kk)W.__ffKeys.key=kk;var ap=g('x-algolia-application-id');if(ap)W.__ffKeys.app=ap}}catch(e){}return oF.apply(this,arguments)}}"
    "}"
    "var F=W.__ffKeys;var o=[F.dom,F.app,F.key,F.idx].join('|');"
    "if(F.app&&F.key&&F.idx){"
    "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(o).then(function(){window.prompt('COPIED! Paste this into the Vault:',o)},function(){window.prompt('Copy this line into the Vault:',o)})}"
    "else{window.prompt('Copy this line into the Vault:',o)}"
    "}else{"
    "window.prompt('Almost! Got the App ID + Index, but not the Search Key yet.\\n\\nType any letter into THIS dealer\\'s inventory SEARCH box so a live search runs, then click this bookmark AGAIN.\\n\\n(Partial result - do not paste yet:)',o)"
    "}"
    "}catch(e){alert('Bookmarklet error: '+e.message)}})();"
)

# --- LOGIN ---
def check_password():
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if st.session_state.password_correct:
        return True
    st.title("🔒 Auto-Analyst Login")
    password = st.text_input("Enter Company Password", type="password")
    if st.button("Log In"):
        if password == "tegna2026": 
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False

if not check_password():
    st.stop()

# --- SHARED EXPORT VISUALS (Premion palette) ---
# Charts are drawn with matplotlib so they render to PNG reliably in any
# environment (Plotly needs kaleido, which isn't guaranteed on the host). The
# data prep mirrors the on-screen dashboard exactly, so exports match the app.
PREMION_NAVY = "#0A1F5C"
PREMION_PERIWINKLE = "#8593E8"
PREMION_ORANGE = "#E8743B"
PREMION_BLUE = "#2E6FB7"
PREMION_GREEN = "#5FBFA0"
PREMION_RED = "#C0504D"
# Fixed colours per category so every chart (dashboard, PDF, deck) agrees.
TYPE_COLORS = {'New': PREMION_BLUE, 'Used': PREMION_RED}
TIER_ORDER = ["Budget (<$30k)", "Core ($30k-$60k)", "Premium ($60k+)"]
TIER_COLORS = {"Budget (<$30k)": PREMION_GREEN, "Core ($30k-$60k)": PREMION_ORANGE,
               "Premium ($60k+)": PREMION_PERIWINKLE}

# Trim/drivetrain tokens used to roll vehicle names up to Make + Model for the
# aggregated "Top Sold/Missed Models" views. Detail tables keep the full name.
TRIM_TOKENS = {
    'lt', 'ltz', 'ls', 'rs', 'ss', 'rst', 'z71', 'zr2', 'premier', 'custom',
    'high', 'denali', 'elevation', 'slt', 'sle', 'at4', 'pro', 'limited',
    'laramie', 'big', 'horn', 'tradesman', 'rebel', 'trx', 'sport', 'touring',
    'ex', 'lx', 'se', 'sel', 'sv', 'sl', 's', 'xle', 'xse', 'sr', 'sr5', 'trd',
    'platinum', 'lariat', 'xlt', 'xl', 'king', 'ranch', 'raptor', 'tremor',
    'wildtrak', 'badlands', 'willys', 'rubicon', 'sahara', 'overland',
    'summit', 'trailhawk', 'latitude', 'altitude', 'fwd', 'awd', 'rwd', '4wd',
    '4x4', '4x2', '2wd', 'front', 'rear', 'four', 'all', 'wheel', 'drive',
    'quad', 'crew', 'double', 'extended', 'regular', 'cab', 'ultimate',
    'reserve', 'signature', 'premium', 'work', 'truck', 'trail', 'boss',
    'activ', 'redline', 'midnight', 'north', 'edition', 'hybrid', 'plug-in',
    'phev', 'ev', 'diesel', 'turbo', 'plus', 'select', 'preferred', 'essence',
    'avenir', 'st', 'st-line', 'titanium', 'calligraphy', 'ultimate,',
}

def normalize_model(name):
    """'2025 Chevrolet Silverado 1500 Rst Four Wheel...' -> 'Chevrolet Silverado 1500'."""
    s = re.sub(r'^\d{4}\s+', '', str(name)).strip()
    tokens = s.split()
    keep = []
    for i, t in enumerate(tokens):
        if i >= 1 and t.lower().strip('.,') in TRIM_TOKENS:
            break
        keep.append(t)
    if len(keep) < 2 and len(tokens) >= 2:
        keep = tokens[:2]
    return ' '.join(keep) if keep else s


def clean_report_title(report_id):
    """Report name without the run time: 'Ted Britt Auto Group (03:04 PM ET)' ->
    'Ted Britt Auto Group'. The time stays in Session History and file names,
    where reps use it to tell runs apart; clients see the clean name."""
    t = re.sub(r'\s*\(\d{1,2}:\d{2}\s*[AP]M[^)]*\)', '', str(report_id))
    t = re.sub(r'\s*-?\s*\d{1,2}:\d{2}\s*[AP]M(\s*ET)?\s*$', '', t)
    return t.strip() or str(report_id)

def report_month_options(n_months=18):
    """None (not specified) followed by the last n_months, newest first."""
    today = datetime.date.today()
    opts, y, m = [None], today.year, today.month
    for _ in range(n_months):
        opts.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return opts

def format_report_month(v):
    return "— Not specified —" if v is None else datetime.date(v[0], v[1], 1).strftime("%B %Y")

def build_report_context(min_visitors=1, report_month=None, unique_visitors=0,
                         total_visits=0, scan_dt=None):
    """
    Optional reporting context for the dashboard and both exports:
      header_meta    - compact one-liner for the title area (report month, scan
                       date, lookback) — metadata, not a sentence
      lookback_chip  - "24-day lookback" for tight spaces
      threshold_line - footnote qualifying the KPI numbers
      unique_visitors_fmt / avg_pages - engagement stats, displayed as figures
    Missing inputs simply produce fewer keys.
    """
    ctx = {}
    try:
        if min_visitors and int(min_visitors) > 1:
            ctx['threshold_line'] = (f"Based on vehicles that received {int(min_visitors)}+ "
                                     f"unique visits from the Premion campaign.")
        if scan_dt is None:
            try:
                scan_dt = datetime.datetime.now(pytz.timezone("US/Eastern"))
            except Exception:
                scan_dt = datetime.datetime.now()
        ctx['scan_date'] = scan_dt.strftime("%B %d, %Y").replace(" 0", " ")
        meta = []
        if report_month:
            y, mo = int(report_month[0]), int(report_month[1])
            month_label = datetime.date(y, mo, 1).strftime("%B %Y")
            month_start = datetime.date(y, mo, 1)
            nxt = datetime.date(y + (mo == 12), (mo % 12) + 1, 1)
            month_end = nxt - datetime.timedelta(days=1)
            days_close = (scan_dt.date() - month_end).days
            ctx['report_month'] = month_label
            ctx['lookback_days'] = days_close
            meta.append(f"{month_label} campaign")
            if days_close >= 0:
                ctx['lookback_chip'] = f"{days_close}-day lookback"
                ctx['lookback_short'] = f"{days_close} days post-close"
                ctx['lookback_help'] = (f"Days between the close of {month_label} and this "
                                        f"inventory scan ({ctx['scan_date']}).")
            else:
                ctx['lookback_chip'] = "month in progress"
                ctx['lookback_short'] = "Month in progress"
        meta.append(f"scanned {ctx['scan_date']}")
        if ctx.get('lookback_chip'):
            meta.append(ctx['lookback_chip'])
        ctx['header_meta'] = "  ·  ".join(meta)
        if unique_visitors and int(unique_visitors) > 0:
            uv = int(unique_visitors)
            ctx['unique_visitors'] = uv
            ctx['unique_visitors_fmt'] = f"{uv:,}"
            tv = int(total_visits or 0)
            if tv > 0:
                avg = tv / uv
                if 0 < avg < 1000:      # guard against a mistyped visitor count
                    ctx['avg_pages'] = f"{avg:.1f}"
                    ctx['total_visits_fmt'] = f"{tv:,}"
    except Exception:
        pass
    return ctx

def _ctx_has_engagement(metrics):
    ctx = (metrics or {}).get('context') or {}
    return bool(ctx.get('unique_visitors_fmt') and ctx.get('avg_pages'))

def _fig_to_png_bytes(fig, w_in, h_in, tight=True):
    fig.set_size_inches(w_in, h_in)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150,
                bbox_inches=("tight" if tight else None),
                facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf

def _donut_chart(values, labels, colors, title, center_top, center_sub):
    """
    Fixed-geometry donut: the axes box is pinned so every donut renders the
    same circle size regardless of label/legend length (saved with tight=False).
    """
    fig, ax = plt.subplots(figsize=(4.0, 4.0))
    fig.patch.set_facecolor("white")
    ax.set_position([0.06, 0.20, 0.88, 0.66])
    total = sum(values)
    wedges, _texts, autotexts = ax.pie(
        values, colors=colors, startangle=90, counterclock=False,
        autopct=lambda p: f"{p:.0f}%" if p >= 5 else "",
        pctdistance=0.78,
        wedgeprops={'width': 0.42, 'edgecolor': 'white', 'linewidth': 2},
        textprops={'color': 'white', 'fontweight': 'bold', 'fontsize': 9})
    ax.text(0, 0.10, center_top, ha='center', va='center',
            fontsize=22, fontweight='bold', color=PREMION_NAVY)
    ax.text(0, -0.24, center_sub, ha='center', va='center',
            fontsize=9, color='#6B7280')
    fig.suptitle(title, y=0.965, fontweight='bold', color=PREMION_NAVY, fontsize=13)
    safe_labels = [str(l).replace('$', r'\$') for l in labels]  # avoid mathtext
    fig.legend(wedges, [f"{l}  ({v})" for l, v in zip(safe_labels, values)],
               loc='lower center', bbox_to_anchor=(0.5, 0.015),
               ncol=2, frameon=False, fontsize=8.5)
    return _fig_to_png_bytes(fig, 4.0, 4.0, tight=False)

def build_summary_chart_images(df, sold_df):
    """
    Render the three Executive Summary charts (Traffic Mix, Sales Mix, Sold Value
    Tiers) to PNG bytes for embedding in PDF/PPTX. Returns a dict of BytesIO
    (keys may be absent when there's no sold data). Never raises.
    """
    images = {}
    try:
        traffic = (df.groupby('Category')['Attributed Unique Visitors'].sum()
                     .reset_index().sort_values('Attributed Unique Visitors', ascending=True))
        fig, ax = plt.subplots()
        fig.patch.set_facecolor("white")
        colors = [PREMION_BLUE] * len(traffic)
        if colors:
            colors[-1] = PREMION_NAVY  # spotlight the top category
        bars = ax.barh(traffic['Category'], traffic['Attributed Unique Visitors'],
                       color=colors, height=0.62)
        ax.bar_label(bars, padding=4, fontsize=9, fontweight='bold', color=PREMION_NAVY)
        ax.set_title("Traffic Mix (Unique Visits by Page Type)", loc='left',
                     fontweight='bold', color=PREMION_NAVY, fontsize=13)
        ax.tick_params(axis='y', labelsize=9)
        ax.set_xticks([])
        for s in ('top', 'right', 'bottom'):
            ax.spines[s].set_visible(False)
        ax.spines['left'].set_color('#D1D5DB')
        max_v = traffic['Attributed Unique Visitors'].max()
        if pd.notna(max_v) and max_v > 0:
            ax.set_xlim(0, max_v * 1.15)
        ax.margins(y=0.02)
        images['traffic'] = _fig_to_png_bytes(fig, 6.6, 3.7)
    except Exception:
        pass
    if sold_df is not None and not sold_df.empty:
        try:
            tc = sold_df['Type'].value_counts()
            cmap = {'New': PREMION_BLUE, 'Used': PREMION_RED}
            images['salesmix'] = _donut_chart(
                tc.values.tolist(), tc.index.tolist(),
                [cmap.get(t, PREMION_PERIWINKLE) for t in tc.index],
                "Sales Mix (New vs Used)", str(int(tc.sum())), "Sold")
        except Exception:
            pass
        try:
            tier = sold_df['Price Tier'].value_counts()
            tier = tier.reindex([t for t in TIER_ORDER if t in tier.index]
                                + [t for t in tier.index if t not in TIER_ORDER])
            images['tiers'] = _donut_chart(
                tier.values.tolist(), tier.index.tolist(),
                [TIER_COLORS.get(t, PREMION_BLUE) for t in tier.index],
                "Sold Value Tiers", str(int(tier.sum())), "Sold")
        except Exception:
            pass
    return images

def build_group_chart_images(dealer_group, top_n=10):
    """
    Render the two Auto Group dashboard charts (Top Dealers by Traffic / by
    Attributed Sales) to PNG bytes for the PDF and PPTX. Horizontal bars so
    long dealer names stay readable. Returns {} on any failure.
    """
    images = {}
    if dealer_group is None or dealer_group.empty:
        return images
    specs = [('group_traffic', 'Total Visitors', 'Top Dealers by Traffic', PREMION_BLUE),
             ('group_sales', 'Units Sold', 'Top Dealers by Viewed Vehicles Sold', PREMION_BLUE)]
    for key, col, title, color in specs:
        try:
            if col not in dealer_group.columns:
                continue
            d = (dealer_group[['Dealer', col]].copy()
                 .sort_values(col, ascending=False).head(top_n)
                 .sort_values(col, ascending=True))
            if d.empty or float(d[col].max()) <= 0:
                continue
            labels = [str(x)[:34] for x in d['Dealer']]
            fig, ax = plt.subplots()
            fig.patch.set_facecolor("white")
            colors = [color] * len(d)
            colors[-1] = PREMION_NAVY          # spotlight the leader
            bars = ax.barh(labels, d[col], color=colors, height=0.62)
            fmt = (lambda v: f"{v:,.0f}")
            ax.bar_label(bars, labels=[fmt(v) for v in d[col]], padding=4,
                         fontsize=9, fontweight='bold', color=PREMION_NAVY)
            ax.set_title(title, loc='left', fontweight='bold',
                         color=PREMION_NAVY, fontsize=13)
            ax.tick_params(axis='y', labelsize=9)
            ax.set_xticks([])
            for s in ('top', 'right', 'bottom'):
                ax.spines[s].set_visible(False)
            ax.spines['left'].set_color('#D1D5DB')
            ax.set_xlim(0, float(d[col].max()) * 1.18)
            ax.margins(y=0.02)
            images[key] = _fig_to_png_bytes(fig, 6.2, 4.3)
        except Exception:
            continue
    return images

# --- HIGH-ENGAGEMENT SALES (the T+ campaign-visit subset) ---
# The full report stays the primary view; vehicles that drew T+ campaign visits
# are layered on top as a conservative, complementary subset. Honesty guards:
#  - a look-to-book comparison is only drawn when both sides have enough viewed
#    vehicles, and only exported when the lift is real and holds like-for-like
#    (a lift that comes purely from the new/used mix is not shown to clients)
#  - exports skip the subset entirely when too few vehicles clear the bar
HE_MIN_GROUP_VIEWED = 20     # viewed vehicles needed on EACH side of a comparison
HE_MIN_LIFT_PTS = 5.0        # look-to-book points the T+ group must lead by (all vehicles)
HE_MIN_SOLD_FOR_EXPORT = 3   # fewer high-engagement sales than this -> exports skip it

def high_engagement_stats(vdp_all, threshold):
    """
    Figures for the high-engagement subset of ALL viewed vehicles (vdp_all must
    be unfiltered). Returns None when there is no subset to describe.
    """
    try:
        t = int(threshold or 0)
    except Exception:
        return None
    if t < 2 or vdp_all is None or vdp_all.empty:
        return None
    vis = pd.to_numeric(vdp_all['Attributed Unique Visitors'], errors='coerce').fillna(0)
    sold = vdp_all['Is Sold'].astype(bool)
    hi = vis >= t
    val = pd.to_numeric(vdp_all['Est. Value'], errors='coerce').fillna(0)
    total_v, total_s = int(len(vdp_all)), int(sold.sum())
    hv, hs = int(hi.sum()), int((hi & sold).sum())
    lv, ls = total_v - hv, total_s - hs
    pct = lambda s, v: round(s / v * 100, 1) if v else None
    he = {
        'threshold': t,
        'total_viewed': total_v, 'total_sold': total_s,
        'viewed': hv, 'sold': hs, 'ltb': pct(hs, hv),
        'lo_viewed': lv, 'lo_sold': ls, 'lo_ltb': pct(ls, lv),
        'value_sold': float(val[hi & sold].sum()),
        'value_viewed': float(val[hi].sum()),
        'sold_share_pct': pct(hs, total_s),
    }
    he['lift_pts'] = (round(he['ltb'] - he['lo_ltb'], 1)
                      if he['ltb'] is not None and he['lo_ltb'] is not None else None)
    he_sold_rows = vdp_all[hi & sold]
    _hv_sold = vis[hi & sold]
    he['avg_visits_sold'] = round(float(_hv_sold.mean()), 1) if len(_hv_sold) else None
    he['sold_type_counts'] = {str(k): int(v) for k, v in he_sold_rows['Type'].value_counts().items()}
    _all_sold_rows = vdp_all[sold]
    he['all_type_counts'] = {str(k): int(v) for k, v in _all_sold_rows['Type'].value_counts().items()}
    he['all_tier_counts'] = {str(k): int(v) for k, v in _all_sold_rows['Price Tier'].value_counts().items()}
    he['sold_tier_counts'] = {str(k): int(v) for k, v in he_sold_rows['Price Tier'].value_counts().items()}

    # Comparison clusters: all vehicles, then like-for-like by condition.
    groups = []
    for label, mask in (("All viewed vehicles", pd.Series(True, index=vdp_all.index)),
                        ("New vehicles only", vdp_all['Type'] == 'New'),
                        ("Used vehicles only", vdp_all['Type'] == 'Used')):
        g_hv, g_hs = int((mask & hi).sum()), int((mask & hi & sold).sum())
        g_lv, g_ls = int((mask & ~hi).sum()), int((mask & ~hi & sold).sum())
        if g_hv < HE_MIN_GROUP_VIEWED or g_lv < HE_MIN_GROUP_VIEWED:
            continue
        if label != "All viewed vehicles" and g_hv == hv and g_lv == lv:
            continue                          # single-condition store: same as "All"
        groups.append({'label': label, 'hi_viewed': g_hv, 'hi_sold': g_hs, 'hi_ltb': pct(g_hs, g_hv),
                       'lo_viewed': g_lv, 'lo_sold': g_ls, 'lo_ltb': pct(g_ls, g_lv),
                       'lift_pts': round(pct(g_hs, g_hv) - pct(g_ls, g_lv), 1)})
    he['groups'] = groups
    # The all-vehicles comparison regardless of sample size (dashboard only).
    he['all_group'] = ({'label': "All viewed vehicles", 'hi_viewed': hv, 'hi_sold': hs,
                        'hi_ltb': he['ltb'], 'lo_viewed': lv, 'lo_sold': ls,
                        'lo_ltb': he['lo_ltb'], 'lift_pts': he['lift_pts']}
                       if hv and lv else None)
    all_g = next((g for g in groups if g['label'] == "All viewed vehicles"), None)
    he['comparison_ok'] = bool(
        all_g and all_g['lift_pts'] >= HE_MIN_LIFT_PTS
        and all(g['lift_pts'] > 0 for g in groups))
    he['export_ok'] = hs >= HE_MIN_SOLD_FOR_EXPORT
    return he

def he_comparison_reason(he):
    """Plain-English reason the look-to-book comparison is held back from exports."""
    t = he['threshold']
    all_g = next((g for g in he.get('groups', []) if g['label'] == "All viewed vehicles"), None)
    if not all_g:
        return (f"too few vehicles on one side of {t} visits to compare fairly "
                f"(needs {HE_MIN_GROUP_VIEWED}+ viewed on each side).")
    if all_g['lift_pts'] < HE_MIN_LIFT_PTS:
        return (f"high-engagement vehicles aren't selling meaningfully faster in this report "
                f"({all_g['lift_pts']:+.1f} pts). That's common: a car that sits longer keeps "
                f"collecting visits, and showroom cars draw browsers, so look-to-book often dips "
                f"as the threshold rises. The strip shows avg. visits per sale instead.")
    bad = [g['label'].replace(' only', '').lower() for g in he['groups'] if g['lift_pts'] <= 0]
    return (f"the gap comes from the new/used mix, not engagement - among "
            f"{' and '.join(bad)} the high-engagement vehicles didn't sell faster.")

def he_ltb_plotly(he):
    """Dashboard version of the look-to-book comparison (rep-facing, so it is
    shown even when the export guard holds it back; sample sizes are labelled)."""
    groups = he.get('groups') or ([he['all_group']] if he.get('all_group') else [])
    if not groups:
        return None
    t = he['threshold']
    fig = go.Figure()
    for side, name, col in (('lo', f"1-{t - 1} visits", PREMION_PERIWINKLE),
                            ('hi', f"{t}+ visits", PREMION_NAVY)):
        fig.add_bar(
            x=[g['label'] for g in groups], y=[g[f'{side}_ltb'] for g in groups],
            name=name, marker_color=col,
            text=[f"{g[f'{side}_ltb']:.1f}%" for g in groups], textposition='outside',
            customdata=[g[f'{side}_viewed'] for g in groups],
            hovertemplate="%{x}<br>" + name + ": %{y:.1f}% look-to-book<br>%{customdata} viewed<extra></extra>")
    top = max(max(g['hi_ltb'], g['lo_ltb']) for g in groups)
    fig.update_layout(
        barmode='group', height=250, margin=dict(l=0, r=0, t=28, b=0),
        legend=dict(orientation='h', y=1.02, x=0, xanchor='left', yanchor='bottom'),
        yaxis=dict(visible=False, range=[0, top * 1.3 if top else 10]),
        plot_bgcolor='white')
    return fig

def mix_compare_plotly(all_counts, he_counts, order, colors, threshold):
    """Two 100% bars - all sold vs. the T+ visit sales - in the same colours as
    the pie above them, so the shift in mix reads at a glance."""
    all_n, he_n = sum(all_counts.values()), sum(he_counts.values())
    if not all_n or not he_n:
        return None
    cats = [c for c in order if all_counts.get(c) or he_counts.get(c)]
    cats += [c for c in set(all_counts) | set(he_counts) if c not in cats]
    rows = [f"All sold ({all_n:,})", f"{threshold}+ visits ({he_n:,})"]
    fig = go.Figure()
    for c in cats:
        shares = [all_counts.get(c, 0) / all_n * 100, he_counts.get(c, 0) / he_n * 100]
        fig.add_bar(y=rows, x=shares, orientation='h', name=str(c),
                    marker_color=colors.get(c, PREMION_BLUE),
                    text=[f"{v:.0f}%" if v >= 9 else "" for v in shares], textposition='inside',
                    insidetextfont=dict(color='white'),
                    customdata=[all_counts.get(c, 0), he_counts.get(c, 0)],
                    hovertemplate=f"{c}: " + "%{x:.0f}% (%{customdata})<extra></extra>")
    fig.update_layout(barmode='stack', height=150, showlegend=False,
                      margin=dict(l=0, r=0, t=4, b=0),
                      xaxis=dict(visible=False, range=[0, 100]),
                      yaxis=dict(autorange='reversed'), plot_bgcolor='white')
    return fig

def he_lift_phrase(he):
    """'(vs. 44.9% at 1-4 visits)' — only when the comparison holds."""
    if not he or not he.get('comparison_ok'):
        return ""
    return f"vs. {he['lo_ltb']:.1f}% at 1-{he['threshold'] - 1} visits"

def build_high_engagement_images(he):
    """
    Export visuals for the high-engagement subset: a viewed -> sold -> T+ sold
    funnel, plus EITHER the look-to-book comparison (when it holds) OR the
    subset's value-tier mix. Returns {} on failure or when there's no subset.
    """
    images = {}
    if not he or not he.get('export_ok'):
        return images
    t = he['threshold']
    try:
        fig, ax = plt.subplots()
        fig.patch.set_facecolor("white")
        rows = [("Vehicles our audience viewed", he['total_viewed'], "#C7CEF5"),
                ("Viewed vehicles sold", he['total_sold'], PREMION_PERIWINKLE),
                (f"Sold with {t}+ campaign visits", he['sold'], PREMION_NAVY)]
        top = max(he['total_viewed'], 1)
        for i, (lab, v, c) in enumerate(rows):
            y = (len(rows) - 1 - i) * 1.25
            ax.barh(y, v, color=c, height=0.55)
            ax.text(v + top * 0.012, y, f"{v:,}", va="center", ha="left",
                    fontsize=14, fontweight="bold", color=PREMION_NAVY)
            ax.text(0, y + 0.43, lab, va="center", ha="left", fontsize=10.5, color="#333333")
        ax.set_xlim(0, top * 1.15); ax.set_ylim(-0.45, 3.0)
        ax.axis("off")
        fig.suptitle(f"Viewed \u2192 Sold \u2192 {t}+ Visits", fontsize=13,
                     fontweight="bold", color=PREMION_NAVY, y=0.95)
        fig.subplots_adjust(left=0.04, right=0.98, top=0.83, bottom=0.04)
        images['he_funnel'] = _fig_to_png_bytes(fig, 5.6, 3.9, tight=False)
    except Exception:
        pass
    if he.get('comparison_ok'):
        try:
            groups = he['groups']
            fig, ax = plt.subplots()
            fig.patch.set_facecolor("white")
            x = list(range(len(groups))); w = 0.36
            series = ((-w / 2, 'lo', PREMION_PERIWINKLE, f"1-{t - 1} visits"),
                      (w / 2, 'hi', PREMION_NAVY, f"{t}+ visits"))
            for off, side, col, lab in series:
                vals = [g[f'{side}_ltb'] for g in groups]
                ns = [g[f'{side}_viewed'] for g in groups]
                bars = ax.bar([i + off for i in x], vals, w, color=col, label=lab)
                for b, v, n in zip(bars, vals, ns):
                    ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:.1f}%", ha="center",
                            va="bottom", fontsize=12, fontweight="bold", color=PREMION_NAVY)
                    ax.text(b.get_x() + b.get_width() / 2, 2.5, f"{n} viewed", ha="center",
                            va="bottom", fontsize=8, color="white")
            ax.set_xticks(x); ax.set_xticklabels([g['label'] for g in groups], fontsize=10.5, color="#333333")
            top = max([g['hi_ltb'] for g in groups] + [g['lo_ltb'] for g in groups])
            ax.set_ylim(0, max(top * 1.25, 10)); ax.set_yticks([])
            for s in ('top', 'right', 'left'):
                ax.spines[s].set_visible(False)
            ax.spines['bottom'].set_color('#D1D5DB')
            ax.legend(frameon=False, fontsize=9.5, loc="upper center", ncol=2, bbox_to_anchor=(0.5, 1.04))
            fig.suptitle("Look-to-Book by Campaign Engagement", fontsize=13,
                         fontweight="bold", color=PREMION_NAVY, y=0.95)
            fig.subplots_adjust(left=0.04, right=0.98, top=0.76, bottom=0.10)
            images['he_ltb'] = _fig_to_png_bytes(fig, 5.6, 3.9, tight=False)
        except Exception:
            pass
    else:
        try:
            images['he_mix'] = _he_mix_bars_image(he)
        except Exception:
            pass
    return images

def _he_mix_bars_image(he):
    """Export version of the dashboard's 'All sold vs. T+ visit sales' bars:
    two 100% bars each for New/Used and Value Tier, same colours as the donuts."""
    t = he['threshold']
    specs = [("Sales Mix (New vs Used)", he.get('all_type_counts') or {}, he.get('sold_type_counts') or {},
              ['New', 'Used'], TYPE_COLORS),
             ("Sold Value Tiers", he.get('all_tier_counts') or {}, he.get('sold_tier_counts') or {},
              TIER_ORDER, TIER_COLORS)]
    fig, axes = plt.subplots(2, 1)
    fig.patch.set_facecolor("white")
    for ax, (title, allc, hec, order, cmap) in zip(axes, specs):
        all_n, he_n = sum(allc.values()), sum(hec.values())
        rows = [f"{t}+ visits ({he_n:,})", f"All sold ({all_n:,})"]
        cats = [c for c in order if allc.get(c) or hec.get(c)] + \
               [c for c in set(allc) | set(hec) if c not in order]
        left = [0.0, 0.0]
        for c in cats:
            vals = [hec.get(c, 0) / he_n * 100 if he_n else 0, allc.get(c, 0) / all_n * 100 if all_n else 0]
            ax.barh(rows, vals, left=left, color=cmap.get(c, PREMION_BLUE), height=0.62,
                    label=str(c).replace('$', r'\$'))
            for j, v in enumerate(vals):
                if v >= 9:
                    ax.text(left[j] + v / 2, j, f"{v:.0f}%", ha='center', va='center',
                            color='white', fontsize=9, fontweight='bold')
            left = [l + v for l, v in zip(left, vals)]
        ax.set_xlim(0, 100); ax.set_xticks([])
        for sp in ('top', 'right', 'bottom', 'left'):
            ax.spines[sp].set_visible(False)
        ax.tick_params(axis='y', length=0, labelsize=9.5)
        ax.set_title(title, loc='left', fontsize=10.5, fontweight='bold', color=PREMION_NAVY)
        ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.02), ncol=len(cats), frameon=False, fontsize=8)
    fig.suptitle(f"All Sold vs. {t}+ Visit Sales", fontsize=13, fontweight="bold",
                 color=PREMION_NAVY, y=0.97)
    fig.subplots_adjust(left=0.24, right=0.97, top=0.80, bottom=0.07, hspace=0.8)
    return _fig_to_png_bytes(fig, 5.6, 3.9, tight=False)

def he_third_stat(he):
    """Third figure on the summary strip. Look-to-book only when the comparison
    holds; otherwise avg. campaign visits per high-engagement sale, so a client
    never sees a lower rate next to the headline look-to-book."""
    if he.get('comparison_ok') and he.get('ltb') is not None:
        lo = f"{he['lo_ltb']:.1f}%"
        return (f"{he['ltb']:.1f}%", f"look-to-book  (vs. {lo} at 1-{he['threshold'] - 1} visits)",
                f"look-to-book (1-{he['threshold'] - 1} visits: {lo})")
    if he.get('avg_visits_sold') is not None:
        return (f"{he['avg_visits_sold']:.0f}", "avg. campaign visits per sale",
                "avg. campaign visits per sale")
    return None

def he_stat_chips(he):
    """Three short facts about the subset for the export stat row."""
    chips = [(f"${he['value_sold']:,.0f}", "est. value of high-engagement sales")]
    tc = he.get('sold_type_counts') or {}
    if he['sold']:
        cond, n = max(tc.items(), key=lambda kv: kv[1]) if tc else (None, 0)
        if cond:
            chips.append((f"{round(100 * n / he['sold'])}% {cond.lower()}",
                          f"of high-engagement sales were {cond.lower()} vehicles"))
        tiers = he.get('sold_tier_counts') or {}
        if tiers:
            tier, tn = max(tiers.items(), key=lambda kv: kv[1])
            chips.append((f"{round(100 * tn / he['sold'])}% {tier.split(' (')[0].lower()}",
                          f"priced {tier.split('(')[-1].rstrip(')')}" if '(' in tier else "value tier"))
    return chips[:3]

def ltb_chip_text(metrics):
    """The small 'New: x%  |  Used: y%' chip. Skips a side with no viewed
    vehicles, so a used-only store never shows a misleading 'New: 0.0%'."""
    parts = []
    for label, key, n_key in (("New", 'new_ltb', 'new_viewed'), ("Used", 'used_ltb', 'used_viewed')):
        n = metrics.get(n_key)
        if n is None or n > 0:            # unknown count -> old behaviour
            if metrics.get(key) is not None:
                parts.append(f"{label}: {metrics[key]}%")
    return "  |  ".join(parts) or None

def build_kpi_band_image(metrics):
    """
    Render the Executive Summary top-line as a navy dashboard-style KPI band
    (PNG bytes) for the PDF. Returns None on any failure so the PDF can fall
    back to plain text.
    """
    try:
        fig = plt.figure(figsize=(10.6, 1.6))
        fig.patch.set_facecolor(PREMION_NAVY)
        kpis = [
            ("VIEWED VEHICLES SOLD", f"{int(metrics.get('units_sold', 0) or 0):,}"),
            ("EST. VALUE SOLD", f"${float(metrics.get('rev_sold', 0)):,.0f}"),
            ("EST. TOTAL VALUE VIEWED", f"${float(metrics.get('pipeline', 0)):,.0f}"),
            ("LOOK-TO-BOOK RATIO", f"{metrics.get('ltb', 0)}%"),
        ]
        for i, (lab, val) in enumerate(kpis):
            x = (i + 0.5) / 4.0
            fig.text(x, 0.72, lab, ha='center', va='center',
                     color='white', fontsize=8.5, fontweight='bold')
            fig.text(x, 0.36, val, ha='center', va='center',
                     color='white', fontsize=21, fontweight='bold')
        _chip = ltb_chip_text(metrics)
        if _chip:
            fig.text(3.5 / 4.0, 0.10, _chip, ha='center', va='center', color='#DDE3F8', fontsize=8)
        import matplotlib.lines as mlines
        for i in (1, 2, 3):
            fig.add_artist(mlines.Line2D([i / 4.0, i / 4.0], [0.18, 0.84],
                                         transform=fig.transFigure,
                                         color=PREMION_PERIWINKLE, alpha=0.35, lw=1))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, facecolor=PREMION_NAVY)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        return None

# Working benchmark: on average a vehicle draws at least this many qualified
# (attributed) VDP visitors before it sells. Used to flag the sold vehicles
# where campaign influence is most likely.
INFLUENCE_BENCHMARK_VISITS = 30
# High-influence bar: half the typical visits-to-sell. A sold vehicle with at
# least this many campaign visits had our campaign supply at least half of its
# typical demand — the sales we most likely influenced. Tune here only.
INFLUENCE_THRESHOLD_VISITS = INFLUENCE_BENCHMARK_VISITS // 2

# One client-facing glossary, used by both the PDF and the PowerPoint so the
# wording can't drift. Positioning rule: we INFLUENCE these sales (qualified
# shoppers into the pipeline, faster velocity); we don't claim the purchases.
CLIENT_GLOSSARY = [
    ("Viewed Vehicles Sold",
     "Vehicles our campaign audience viewed on the dealer's site that have since left its live "
     "inventory. They show the campaign sending qualified shoppers into the pipeline "
     "and the pace that inventory is moving - not purchases made by our visitors."),
    ("Est. Value Sold",
     "Directional value of the viewed vehicles that sold. New: base MSRP for the "
     "model. Used: base MSRP depreciated by age (15% year one, 10% each following "
     "year). Not transaction prices; excludes trims, options, and dealer markups."),
    ("Est. Total Value Viewed",
     "The estimated value of every vehicle our audience viewed - sold, still listed, "
     "and unconfirmed - using the same method. It sizes the in-market demand the "
     "campaign reached."),
    ("Est. Value Still on the Lot",
     "The estimated value of the viewed vehicles still listed for sale - the part of "
     "Est. Total Value Viewed that hasn't sold yet. Vehicles whose status couldn't be "
     "confirmed are left out."),
    ("Look-to-Book Ratio",
     "Viewed vehicles sold / all viewed vehicles, split New vs Used - how quickly "
     "the inventory our audience engaged with is moving."),
    ("Traffic Mix",
     "Where audiences navigated on the site: VDPs, Service, Search, Incentives/Offers, "
     "Homepage."),
    ("Missed Opportunities (The Watch List)",
     "Active vehicles receiving above-average campaign traffic that haven't sold yet. "
     "Review these VDPs for missing photos, 'Call for Price' buttons, or pricing outliers."),
    ("Methodology Note",
     "Sold-vehicle counts reflect inventory our audience viewed that has since left "
     "the lot; they indicate influence and velocity, not purchases by our visitors. "
     "Inventory status reflects the dealer's website when this report was run, not a "
     "historical snapshot."),
]

def client_glossary(he=None):
    """CLIENT_GLOSSARY, plus the High-Engagement Sales entry when the export
    includes that subset (inserted right after Look-to-Book)."""
    items = list(CLIENT_GLOSSARY)
    if he and he.get('export_ok'):
        entry = ("High-Engagement Sales",
                 f"Viewed vehicles sold that drew {he['threshold']}+ unique visits from the "
                 f"campaign audience - the sales where our audience's engagement was deepest. "
                 f"A conservative subset of Viewed Vehicles Sold; every other figure in this "
                 f"report includes all viewed vehicles.")
        idx = next((i for i, (h, _b) in enumerate(items) if h == "Look-to-Book Ratio"), len(items) - 1)
        items.insert(idx + 1, entry)
    return items

FACTS_SCHEMA_VERSION = 2
APP_BUILD = "2026.10.26"   # bump on release; surfaced in the facts export

# Makes whose names are more than one word, so "make" can be split off the
# front of a normalized vehicle name correctly.
MULTIWORD_MAKES = [
    "MERCEDES BENZ", "LAND ROVER", "ALFA ROMEO", "ASTON MARTIN", "ROLLS ROYCE",
    "GENERAL MOTORS",
]

# Raw-slug make spellings -> the canonical form used for cross-source matching.
MAKE_ALIASES = {
    "MERCEDES BENZ": "MERCEDES-BENZ",
    "LAND ROVER": "LAND ROVER",
    "ALFA ROMEO": "ALFA ROMEO",
    "ROLLS ROYCE": "ROLLS-ROYCE",
    "ASTON MARTIN": "ASTON MARTIN",
    "CHEVY": "CHEVROLET",
    "VW": "VOLKSWAGEN",
    "MAZDA": "MAZDA",
    "GMC": "GMC",
    "BMW": "BMW",
}

def canonical_model(model_text):
    """
    Canonical model string for matching against Polk registrations and website
    page names: uppercase, single-spaced, and short alpha+numeric series
    hyphenated the way the industry writes them ("F 150" -> "F-150",
    "CX 5" -> "CX-5"). Longer words keep their space ("MODEL 3" stays).
    """
    s = re.sub(r'\s+', ' ', str(model_text or "")).strip().upper()
    if not s:
        return None
    s = re.sub(r'\b([A-Z]{1,2}) (\d{2,4}[A-Z]{0,2})\b', r'\1-\2', s)
    return s

def split_make_model(vehicle_name):
    """
    ('FORD', 'F-150', 'Ford F 150 Lariat 4Wd') from a report vehicle name.
    Uses normalize_model first so trims/drivetrain are already stripped.
    Returns (make, model, raw_normalized) with None where unknown.
    """
    raw = normalize_model(vehicle_name)
    up = re.sub(r'\s+', ' ', str(raw or "")).strip().upper()
    if not up:
        return (None, None, None)
    make = None
    for mw in MULTIWORD_MAKES:
        if up.startswith(mw + " ") or up == mw:
            make = mw
            up = up[len(mw):].strip()
            break
    if make is None:
        parts = up.split(" ", 1)
        make = parts[0]
        up = parts[1] if len(parts) > 1 else ""
    make = MAKE_ALIASES.get(make, make)
    return (make or None, canonical_model(up), raw or None)

def _num(v):
    """Plain JSON number, or None. Never returns 0 for missing input."""
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:      # NaN
            return None
        return int(f) if float(f).is_integer() else round(f, 2)
    except Exception:
        return None

def _slug(text):
    return re.sub(r'[^A-Za-z0-9]+', '-', str(text or "report")).strip('-').lower() or "report"

def _he_facts(scope_vdp, threshold):
    """high_engagement block: the T+ campaign-visit subset of the viewed
    vehicles, with the below-threshold group for comparison. Counts of 0 are
    measured findings and are emitted; rates with no denominator are omitted."""
    he = high_engagement_stats(scope_vdp, threshold)
    if not he:
        return None
    blk = {
        'threshold_visits': he['threshold'],
        'viewed': he['viewed'],
        'sold': he['sold'],
        'look_to_book_pct': he['ltb'],
        'est_value_sold': _num(he['value_sold']) if he['sold'] else None,
        'est_value_viewed': _num(he['value_viewed']) if he['viewed'] else None,
        'share_of_sold_pct': he['sold_share_pct'],
        'avg_visits_per_sale': he.get('avg_visits_sold'),
        'below_threshold': {k: v for k, v in {
            'viewed': he['lo_viewed'], 'sold': he['lo_sold'],
            'look_to_book_pct': he['lo_ltb']}.items() if v is not None},
        'lift_pts': he['lift_pts'],
        # True only when the comparison is big enough on both sides, the T+
        # group leads by HE_MIN_LIFT_PTS, and it holds within New and Used.
        'comparison_holds': he['comparison_ok'],
        'min_group_viewed': HE_MIN_GROUP_VIEWED,
    }
    vis = pd.to_numeric(scope_vdp['Attributed Unique Visitors'], errors='coerce').fillna(0)
    sold = scope_vdp['Is Sold'].astype(bool)
    hi = vis >= he['threshold']
    conds = []
    for cond in ('New', 'Used'):
        m = scope_vdp['Type'] == cond
        hv, hs = int((m & hi).sum()), int((m & hi & sold).sum())
        lv, ls = int((m & ~hi).sum()), int((m & ~hi & sold).sum())
        if not (hv or lv):
            continue
        row = {'condition': cond.upper(), 'viewed': hv, 'sold': hs,
               'look_to_book_pct': round(hs / hv * 100, 1) if hv else None,
               'below_viewed': lv, 'below_sold': ls,
               'below_look_to_book_pct': round(ls / lv * 100, 1) if lv else None}
        conds.append({k: v for k, v in row.items() if v is not None})
    blk['by_condition'] = conds or None
    tiers = he.get('sold_tier_counts') or {}
    blk['sold_by_price_tier'] = [
        {'tier': _slug(k).upper().replace('-', '_'), 'label': k, 'count': v}
        for k, v in tiers.items()] or None
    return {k: v for k, v in blk.items() if v is not None}

def _facts_block(scope_df, scope_vdp, scope_sold, scope_missed, he_threshold=None):
    """
    The metrics block for one scope (a single site, or the whole group).
    Every figure here is computed from the same frames that feed the deck.
    Missing data is None or an omitted key — never 0.
    """
    block = {}
    shopped = len(scope_vdp)
    sold_n = len(scope_sold)
    block['vehicles_shopped'] = shopped if shopped else None
    block['vehicles_sold'] = sold_n if sold_n else None
    block['look_to_book_pct'] = round(sold_n / shopped * 100, 1) if shopped else None

    for label, cond in (('new', 'New'), ('used', 'Used')):
        pool = scope_vdp[scope_vdp['Type'] == cond]
        hit = scope_sold[scope_sold['Type'] == cond]
        block[f'look_to_book_pct_{label}'] = (round(len(hit) / len(pool) * 100, 1)
                                              if len(pool) else None)
        block[f'vehicles_sold_{label}'] = len(hit) if len(hit) else None

    # New vs Used sell-through gap, for the report builder to state in prose.
    # Signed percentage POINTS (used minus new); only when both sides exist.
    _n, _u = block.get('look_to_book_pct_new'), block.get('look_to_book_pct_used')
    if _n is not None and _u is not None:
        block['look_to_book_gap_pts'] = round(_u - _n, 1)
        block['faster_condition'] = ('USED' if _u - _n >= 1.0 else
                                     'NEW' if _n - _u >= 1.0 else 'EVEN')
    block['est_revenue_sold'] = _num(scope_sold['Est. Value'].sum()) if sold_n else None
    block['est_pipeline_value'] = _num(scope_vdp['Est. Value'].sum()) if shopped else None
    # v2: the part of the shopped value still listed for sale. Unconfirmed
    # vehicles are excluded — we don't know they're still on the lot.
    _still_listed = scope_vdp[scope_vdp['Sold_Status'].astype(str) == 'Available']
    block['est_unsold_value'] = (_num(_still_listed['Est. Value'].sum())
                                 if len(_still_listed) else None)
    block['avg_sold_price_est'] = _num(scope_sold['Est. Value'].mean()) if sold_n else None
    # days_on_lot / body_style are not computed by this app -> intentionally absent

    visits = _num(scope_df['Attributed Unique Visitors'].sum())
    block['visits_total'] = visits if visits else None

    if sold_n:
        makes = {}
        models = {}
        for _, r in scope_sold.iterrows():
            mk, md, raw = split_make_model(r.get('Vehicle Name'))
            if mk:
                makes[mk] = makes.get(mk, 0) + 1
            if mk and md:
                key = (mk, md)
                ent = models.setdefault(key, {'count': 0, 'raw': raw})
                ent['count'] += 1
        block['sold_by_make'] = [{'make': k, 'count': v}
                                 for k, v in sorted(makes.items(), key=lambda kv: -kv[1])] or None
        rows = []
        for (mk, md), ent in sorted(models.items(), key=lambda kv: -kv[1]['count']):
            row = {'make': mk, 'model': md, 'label': f"{mk} {md}", 'count': ent['count']}
            if ent['raw'] and ent['raw'].upper() != f"{mk} {md}":
                row['raw_name'] = ent['raw']
            rows.append(row)
        block['sold_by_make_model'] = rows or None
        block['top_sellers'] = rows[:5] or None

        tiers = scope_sold['Price Tier'].value_counts()
        block['sold_by_price_tier'] = [
            {'tier': _slug(k).upper().replace('-', '_'), 'label': str(k), 'count': int(v)}
            for k, v in tiers.items()] or None
    else:
        block['sold_by_make'] = None
        block['sold_by_make_model'] = None
        block['top_sellers'] = None
        block['sold_by_price_tier'] = None

    # ---- v2: what the audience SHOPPED (not just what sold) ----
    if shopped:
        tiers_all = scope_vdp['Price Tier'].fillna('Unknown').astype(str).value_counts()
        block['shopped_by_price_tier'] = [
            {'tier': _slug(k).upper().replace('-', '_'), 'label': str(k), 'count': int(v)}
            for k, v in tiers_all.items()] or None
        shop_models = {}
        for _, r in scope_vdp.iterrows():
            mk, md, raw = split_make_model(r.get('Vehicle Name'))
            if mk and md:
                ent = shop_models.setdefault((mk, md), {'count': 0, 'raw': raw})
                ent['count'] += 1
        rows_s = []
        for (mk, md), ent in sorted(shop_models.items(), key=lambda kv: -kv[1]['count']):
            row = {'make': mk, 'model': md, 'label': f"{mk} {md}", 'count': ent['count']}
            if ent['raw'] and ent['raw'].upper() != f"{mk} {md}":
                row['raw_name'] = ent['raw']
            rows_s.append(row)
        block['shopped_by_make_model'] = rows_s or None

    # ---- v2: look-to-book by attributed-visit band (JSON only, no display) ----
    # Tests over time whether more campaign visits go with faster selling. Same
    # definition as the headline LTB: sold / all viewed vehicles in the band.
    # Empty bands are omitted, never reported as 0.
    if shopped:
        _vis = pd.to_numeric(scope_vdp['Attributed Unique Visitors'], errors='coerce')
        _is_sold = scope_vdp['Is Sold'].astype(bool)
        _bands = []
        for _label, _lo, _hi in (("1", 1, 1), ("2-4", 2, 4), ("5-9", 5, 9), ("10+", 10, None)):
            _m = (_vis >= _lo) & ((_vis <= _hi) if _hi is not None else True)
            _n = int(_m.sum())
            if not _n:
                continue
            _s = int((_m & _is_sold).sum())
            _row = {'band': _label, 'min_visits': _lo, 'viewed': _n, 'sold': _s,
                    'look_to_book_pct': round(_s / _n * 100, 1)}
            if _hi is not None:
                _row['max_visits'] = _hi
            _bands.append(_row)
        block['look_to_book_by_visit_band'] = _bands or None

    # ---- v2: influence evidence on the sold units ----
    if sold_n:
        sv = pd.to_numeric(scope_sold['Attributed Unique Visitors'], errors='coerce').dropna()
        # A measured count: 0 here means "no sold vehicle reached the benchmark",
        # which is a real finding, so it's emitted rather than omitted.
        block['sold_above_benchmark'] = int((sv >= INFLUENCE_BENCHMARK_VISITS).sum())
        # High-influence: at least half the typical visits-to-sell came from us.
        block['sold_high_influence'] = int((sv >= INFLUENCE_THRESHOLD_VISITS).sum())
        if len(sv):
            block['sold_visits_distribution'] = {
                'min': _num(sv.min()), 'median': _num(sv.median()), 'max': _num(sv.max())}
        top = scope_sold.assign(_v=pd.to_numeric(scope_sold['Attributed Unique Visitors'],
                                                 errors='coerce')).sort_values('_v', ascending=False).head(10)
        units = []
        for _, r in top.iterrows():
            mk, md, raw = split_make_model(r.get('Vehicle Name'))
            u = {'make': mk, 'model': md,
                 'label': f"{mk} {md}" if mk and md else (raw or None),
                 'attributed_visits': _num(r.get('Attributed Unique Visitors')),
                 'est_value': _num(r.get('Est. Value'))}
            vin = str(r.get('VIN') or '').strip()
            if vin and vin.upper() not in ('N/A', 'NAN'):
                u['vin'] = vin
            units.append({k: v for k, v in u.items() if v is not None})
        block['top_sold_units'] = units or None

    # Status of the shopped vehicles only — NOT the dealer's full inventory.
    statuses = scope_vdp['Sold_Status'].astype(str)
    avail = int((statuses == 'Available').sum())
    unver = int(statuses.str.startswith('ERROR').sum())
    block['shopped_vehicle_status'] = {
        'available': avail or None,
        'sold': sold_n or None,
        'unverified': unver or None,
    } if shopped else None

    traffic = (scope_df.groupby('Category')['Attributed Unique Visitors'].sum()
               .sort_values(ascending=False))
    block['traffic_mix'] = [
        {'category': _slug(k).upper().replace('-', '_'), 'label': str(k), 'visits': int(v)}
        for k, v in traffic.items() if int(v) > 0] or None

    if scope_missed is not None and not scope_missed.empty:
        veh = []
        for _, r in scope_missed.iterrows():
            mk, md, raw = split_make_model(r.get('Vehicle Name'))
            av = _num(r.get('Attributed Unique Visitors'))
            item = {'make': mk, 'model': md,
                    'label': f"{mk} {md}" if mk and md else (raw or None),
                    'visits': av,               # v1 name, kept for v1 readers
                    'attributed_visits': av,
                    'est_value': _num(r.get('Est. Value'))}
            vin = str(r.get('VIN') or '').strip()
            if vin and vin.upper() not in ('N/A', 'NAN'):
                item['vin'] = vin
            veh.append({k: v for k, v in item.items() if v is not None})
        block['missed_opportunities'] = {'count': len(veh), 'vehicles': veh}
    else:
        block['missed_opportunities'] = None

    if he_threshold and shopped:
        block['high_engagement'] = _he_facts(scope_vdp, he_threshold)

    return {k: v for k, v in block.items() if v is not None}

def build_facts_json(df, vdp_df, sold_df, missed_df, metrics, report_id,
                     dealer_group=None, report_ctx=None):
    """
    Machine-readable facts export: the same figures the deck presents, as data.
    Built from the frames that feed the charts and tables, so the file cannot
    disagree with the deck. Numbers and labels only — no narrative.
    """
    report_ctx = report_ctx or {}
    try:
        scan_dt = datetime.datetime.now(pytz.timezone("US/Eastern"))
    except Exception:
        scan_dt = datetime.datetime.now()

    # analysis_period is REQUIRED: the report builder aligns this file against
    # the website attribution and Polk periods, and a facts file without it
    # cannot be safely combined. No report month -> no facts file.
    rm = st.session_state.get('report_month_sel')
    if not rm:
        return None
    y, mo = int(rm[0]), int(rm[1])
    start = datetime.date(y, mo, 1)
    nxt = datetime.date(y + (mo == 12), (mo % 12) + 1, 1)
    period = {
        'start': start.isoformat(),
        'end': (nxt - datetime.timedelta(days=1)).isoformat(),
        'source': 'rep_entered_report_month',
    }

    sites = []
    for dealer in sorted(df['Dealer'].dropna().unique()):
        d_rows = df[df['Dealer'] == dealer]
        domain = None
        for u in d_rows['Page Url'].head(5):
            try:
                domain = urlparse(str(u)).netloc.replace('www.', '').lower()
                if domain:
                    break
            except Exception:
                continue
        d_sold = sold_df[sold_df['Dealer'] == dealer] if not sold_df.empty else sold_df
        brands = []
        if not d_sold.empty:
            seen = {}
            for _, r in d_sold.iterrows():
                mk, _md, _raw = split_make_model(r.get('Vehicle Name'))
                if mk:
                    seen[mk] = seen.get(mk, 0) + 1
            brands = [m for m, _c in sorted(seen.items(), key=lambda kv: -kv[1])]
        site = {'site_id': _slug(dealer), 'dealer_name': str(dealer)}
        if domain:
            site['domain'] = domain
            site['url'] = f"https://www.{domain}/"
            site['is_group_site'] = bool(is_group_site_url(
                f"https://{domain}/", df['Dealer'].nunique() > 1))
        if brands:
            # Derived from the makes actually sold in this report, not a
            # franchise list the app holds.
            site['brands_observed'] = brands
        sites.append(site)

    is_group = df['Dealer'].nunique() > 1
    meta = {
        'generated_at': scan_dt.isoformat(),
        'app_build': APP_BUILD,
        'report_id': str(report_id),
        'is_group': bool(is_group),
        'site_count': int(df['Dealer'].nunique()),
        'inventory_scanned_at': scan_dt.date().isoformat(),
        'analysis_period': period,
        'vdp_visit_threshold': _num(metrics.get('min_visitors')),
        # 'highlight': full report, T+ visit vehicles reported as a subset
        #              (high_engagement blocks); 'filter': every figure is
        #              already limited to vdp_visit_threshold+ vehicles.
        'vdp_filter_mode': metrics.get('vdp_mode') or 'filter',
        'high_engagement_threshold_visits': (_num(metrics.get('he_threshold'))
                                             if metrics.get('vdp_mode') == 'highlight' else None),
        'influence_benchmark_visits': INFLUENCE_BENCHMARK_VISITS,
        'influence_threshold_visits': INFLUENCE_THRESHOLD_VISITS,
    }
    if is_group:
        meta['group_name'] = str(report_id)
    if report_ctx.get('lookback_days') is not None:
        meta['lookback_days'] = int(report_ctx['lookback_days'])
    meta = {k: v for k, v in meta.items() if v is not None}

    _het = metrics.get('he_threshold') if metrics.get('vdp_mode') == 'highlight' else None
    totals = _facts_block(df, vdp_df, sold_df, missed_df, he_threshold=_het)
    uv = report_ctx.get('unique_visitors')
    if uv:
        totals['unique_visitors'] = int(uv)
        if report_ctx.get('avg_pages'):
            totals['avg_pages_per_visitor'] = float(report_ctx['avg_pages'])

    facts = {
        'schema_version': FACTS_SCHEMA_VERSION,
        'meta': meta,
        'sites': sites,
        'totals': totals,
    }

    if is_group:
        by_site = {}
        for dealer in sorted(df['Dealer'].dropna().unique()):
            s_df = df[df['Dealer'] == dealer]
            s_vdp = vdp_df[vdp_df['Dealer'] == dealer]
            s_sold = sold_df[sold_df['Dealer'] == dealer] if not sold_df.empty else sold_df
            s_missed = (missed_df[missed_df['Dealer'] == dealer]
                        if missed_df is not None and not missed_df.empty else None)
            blk = _facts_block(s_df, s_vdp, s_sold, s_missed, he_threshold=_het)
            if blk:
                by_site[_slug(dealer)] = blk
        if by_site:
            facts['by_site'] = by_site

    return facts

def facts_filename(report_id, facts):
    period = (facts.get('meta') or {}).get('analysis_period') or {}
    start = period.get('start') or (facts.get('meta') or {}).get('inventory_scanned_at')
    end = period.get('end') or (facts.get('meta') or {}).get('inventory_scanned_at')
    return f"{_slug(report_id)}_{start}_{end}_facts.json"

def build_pptx_report(df, sold_df, metrics, chart_images, report_title,
                      missed_df=None, dealer_group=None,
                      include_dealer_details=False, include_missed=True):
    """
    Build the full Premion-styled PowerPoint deck: Executive Summary, charts,
    Top Sold, Missed Opportunities (Watch List), Auto Group scoreboard +
    per-dealer deep dives for multi-dealer reports, and Glossary/Methodology.
    Returns PPTX file bytes, or None if python-pptx isn't available.
    """
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor
        from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
        from pptx.enum.shapes import MSO_SHAPE
    except Exception:
        return None

    NAVY = RGBColor(0x0A, 0x1F, 0x5C)
    PERI = RGBColor(0x85, 0x93, 0xE8)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)
    DARK = RGBColor(0x33, 0x33, 0x33)
    GREY = RGBColor(0x6B, 0x72, 0x80)
    LIGHT = RGBColor(0xEF, 0xF1, 0xFA)

    prs = Presentation()
    prs.slide_width = Inches(13.333)   # 16:9
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    def add_text(slide, left, top, width, height, text, size, color,
                 bold=False, align=PP_ALIGN.LEFT, italic=False):
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame; tf.word_wrap = True
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = tf.paragraphs[0]; p.alignment = align
        r = p.add_run(); r.text = text
        r.font.size = Pt(size); r.font.bold = bold; r.font.color.rgb = color
        r.font.italic = italic
        r.font.name = "Arial"
        return tb

    def fill_rect(slide, left, top, width, height, color, rounded=True):
        shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
        shp = slide.shapes.add_shape(shape_type, left, top, width, height)
        shp.fill.solid(); shp.fill.fore_color.rgb = color
        shp.line.fill.background()
        return shp

    def add_footer(slide):
        add_text(slide, Inches(0.5), Inches(7.02), Inches(6), Inches(0.35),
                 "TEGNA  |  PREMION", 12, NAVY, bold=True)

    def new_slide(title, band_h=1.1):
        s = prs.slides.add_slide(blank)
        fill_rect(s, Inches(0), Inches(0), prs.slide_width, Inches(band_h), NAVY,
                  rounded=False)
        add_text(s, Inches(0.5), Inches(0.2), Inches(12.3), Inches(0.7),
                 title, 26, PERI, bold=True)
        add_footer(s)
        return s

    def add_table(slide, left, top, width, rows_data, col_fracs, font_size=10,
                  row_h_in=0.32):
        if not rows_data:
            return None
        n_rows = len(rows_data); n_cols = len(rows_data[0])
        gfx = slide.shapes.add_table(n_rows, n_cols, left, top, width,
                                     Inches(row_h_in * n_rows))
        table = gfx.table
        total = float(sum(col_fracs))
        for j, frac in enumerate(col_fracs):
            table.columns[j].width = int(width * (frac / total))
        for i, row in enumerate(rows_data):
            for j, val in enumerate(row):
                cell = table.cell(i, j)
                cell.margin_left = Pt(4); cell.margin_right = Pt(4)
                cell.margin_top = Pt(1); cell.margin_bottom = Pt(1)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                tf = cell.text_frame; tf.word_wrap = False
                p = tf.paragraphs[0]
                r = p.add_run(); r.text = str(val)
                r.font.name = "Arial"; r.font.size = Pt(font_size)
                if i == 0:
                    r.font.bold = True; r.font.color.rgb = WHITE
                    cell.fill.solid(); cell.fill.fore_color.rgb = NAVY
                else:
                    r.font.color.rgb = DARK
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = LIGHT if i % 2 == 0 else WHITE
        return gfx

    def add_picture_fit(slide, buf, left, top, max_w_in, max_h_in):
        """Place an image inside a bounding box, preserving aspect ratio."""
        try:
            from PIL import Image as PILImage
            buf.seek(0)
            with PILImage.open(buf) as im:
                aspect = im.width / im.height
        except Exception:
            aspect = 1.0
        buf.seek(0)
        w = max_w_in; h = w / aspect
        if h > max_h_in:
            h = max_h_in; w = h * aspect
        pic = slide.shapes.add_picture(buf, Inches(left), Inches(top),
                                       width=Inches(w))
        buf.seek(0)
        return pic

    def _model_counts(frame, count_label, top_n=10):
        if frame is None or frame.empty:
            return pd.DataFrame()
        f = frame.copy()
        f['Model_Only'] = f['Vehicle Name'].apply(normalize_model)
        counts = f['Model_Only'].value_counts().reset_index()
        counts.columns = ['Make/Model', count_label]
        return counts[counts[count_label] > 1].head(top_n)

    def _fmt_money(v):
        try:
            return f"${float(v):,.0f}"
        except Exception:
            return str(v)

    missed_df = missed_df if missed_df is not None else pd.DataFrame()
    has_missed = include_missed and not missed_df.empty
    has_group = dealer_group is not None and not dealer_group.empty

    # --- Slide 1: title band + KPI cards ---
    s1 = prs.slides.add_slide(blank)
    fill_rect(s1, Inches(0), Inches(0), prs.slide_width, Inches(1.6), NAVY,
              rounded=False)
    add_text(s1, Inches(0.5), Inches(0.15), Inches(12.3), Inches(0.8),
             "Premion Website Attribution", 34, PERI, bold=True)
    # Client-facing: show the report DATE, not the pull time.
    display_title = clean_report_title(report_title)
    try:
        report_date = datetime.datetime.now(pytz.timezone("US/Eastern")).strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:
        report_date = datetime.datetime.now().strftime("%B %d, %Y")
    add_text(s1, Inches(0.5), Inches(0.86), Inches(12.3), Inches(0.42),
             f"Inventory Impact Report  |  {display_title}", 18, WHITE, bold=True)
    ctx = (metrics or {}).get('context') or {}
    add_text(s1, Inches(0.5), Inches(1.24), Inches(12.3), Inches(0.3),
             ctx.get('header_meta') or report_date, 12, PERI)
    add_text(s1, Inches(0.5), Inches(1.75), Inches(12.3), Inches(0.5),
             "Complementary to your website attribution: the vehicles our audience "
             "viewed on the dealer's site - and how quickly that inventory is moving.", 13, GREY, italic=True)

    units_val = metrics.get('units_sold', metrics.get('units', 0))
    rev_val = metrics.get('rev_sold', metrics.get('revenue', 0))
    ltb_val = metrics.get('ltb', 0)
    try:
        units_fmt = f"{int(units_val):,}"
    except Exception:
        units_fmt = str(units_val)
    kpis = [("Viewed Vehicles Sold", units_fmt),
            ("Est. Value Sold", _fmt_money(rev_val)),
            ("Est. Total Value Viewed", _fmt_money(metrics.get('pipeline', 0))),
            ("Look-to-Book Ratio", f"{ltb_val}%")]
    card_w = Inches(2.9); card_h = Inches(1.9); gap = Inches(0.28)
    total_w = card_w * 4 + gap * 3
    x0 = (prs.slide_width - total_w) / 2
    y0 = Inches(2.7)
    for i, (label, val) in enumerate(kpis):
        cx = x0 + i * (card_w + gap)
        fill_rect(s1, cx, y0, card_w, card_h, NAVY)
        add_text(s1, cx, y0 + Inches(0.25), card_w, Inches(0.5), label, 12, WHITE,
                 align=PP_ALIGN.CENTER)
        add_text(s1, cx, y0 + Inches(0.8), card_w, Inches(0.9), val, 30, WHITE,
                 bold=True, align=PP_ALIGN.CENTER)
    _chip = ltb_chip_text(metrics)
    if _chip:
        add_text(s1, x0 + 3 * (card_w + gap), y0 + card_h + Inches(0.08),
                 card_w, Inches(0.4), _chip, 11, GREY, align=PP_ALIGN.CENTER)
    he = (metrics or {}).get('high_engagement')
    he_on = bool(he and he.get('export_ok'))
    if ctx.get('threshold_line'):
        # Sits directly under the KPI cards: it qualifies those numbers.
        add_text(s1, Inches(0.5), Inches(5.12), Inches(12.3), Inches(0.32),
                 ctx['threshold_line'], 12, NAVY, align=PP_ALIGN.CENTER)
    ltb_def_top = 5.62
    if he_on:
        # Secondary, lighter strip under the primary navy cards: the
        # high-engagement subset of the headline numbers.
        band = fill_rect(s1, Inches(0.45), Inches(5.22), Inches(12.44), Inches(0.92), LIGHT)
        try:
            band.adjustments[0] = 0.12
        except Exception:
            pass
        fill_rect(s1, Inches(0.45), Inches(5.22), Inches(0.09), Inches(0.92), NAVY, rounded=False)
        add_text(s1, Inches(0.70), Inches(5.28), Inches(3.5), Inches(0.40),
                 "HIGH-ENGAGEMENT SALES", 12, NAVY, bold=True)
        add_text(s1, Inches(0.70), Inches(5.64), Inches(3.5), Inches(0.40),
                 f"Viewed vehicles that drew {he['threshold']}+ campaign visits", 10.5, GREY)
        strip = [(f"{he['sold']:,}", f"of the {he['total_sold']:,} sold"),
                 (_fmt_money(he['value_sold']), "est. value sold")]
        _third = he_third_stat(he)
        if _third:
            strip.append(_third[:2])
        for (big, small), x in zip(strip, (4.25, 6.55, 9.15)):
            add_text(s1, Inches(x), Inches(5.25), Inches(2.4), Inches(0.5), big, 20, NAVY, bold=True)
            add_text(s1, Inches(x), Inches(5.66), Inches(3.7 if x > 9 else 2.4), Inches(0.36),
                     small, 10.5, GREY)
        ltb_def_top = 6.30
    add_text(s1, Inches(0.5), Inches(ltb_def_top), Inches(12.3), Inches(0.5),
             "Look-to-Book = viewed vehicles sold / all viewed vehicles - how "
             "quickly the inventory our audience engaged with is moving.", 12, GREY, italic=True,
             align=PP_ALIGN.CENTER)
    add_footer(s1)

    # --- Slide 2: Traffic Mix (full-size) + engagement stats ---
    if chart_images.get('traffic'):
        s2 = new_slide("Traffic Mix: Where the Audience Shopped")
        if _ctx_has_engagement(metrics):
            add_picture_fit(s2, chart_images['traffic'], 0.4, 1.5, 9.0, 4.9)
            stats = [("Unique Visitors", ctx.get('unique_visitors_fmt', '-')),
                     ("Avg. Pages per Visitor", ctx.get('avg_pages', '-'))]
            for n, (lab, val) in enumerate(stats):
                cy = Inches(1.95) + Inches(1.85) * n
                fill_rect(s2, Inches(9.75), cy, Inches(3.1), Inches(1.55), NAVY)
                add_text(s2, Inches(9.75), cy + Inches(0.2), Inches(3.1), Inches(0.4),
                         lab, 12, WHITE, align=PP_ALIGN.CENTER)
                add_text(s2, Inches(9.75), cy + Inches(0.66), Inches(3.1), Inches(0.7),
                         val, 30, WHITE, bold=True, align=PP_ALIGN.CENTER)
        else:
            add_picture_fit(s2, chart_images['traffic'], 1.5, 1.5, 10.3, 4.9)
        add_text(s2, Inches(0.5), Inches(6.45), Inches(12.3), Inches(0.5),
                 "VDP visits are high-intent inventory shoppers - the audience "
                 "most likely to buy.", 12, GREY, italic=True,
                 align=PP_ALIGN.CENTER)

    # --- Slide 3: Sales Mix & Sold Value Tiers ---
    if chart_images.get('salesmix') or chart_images.get('tiers'):
        s2b = new_slide("What Sold: Sales Mix & Value Tiers")
        if chart_images.get('salesmix') and chart_images.get('tiers'):
            add_picture_fit(s2b, chart_images['salesmix'], 1.55, 1.55, 4.6, 4.6)
            add_picture_fit(s2b, chart_images['tiers'], 7.2, 1.55, 4.6, 4.6)
        else:
            only = chart_images.get('salesmix') or chart_images.get('tiers')
            add_picture_fit(s2b, only, 4.35, 1.55, 4.6, 4.6)
        add_text(s2b, Inches(0.5), Inches(6.45), Inches(12.3), Inches(0.5),
                 "The mix of inventory our traffic helped move - what sold, and "
                 "at what price points.", 12, GREY, italic=True,
                 align=PP_ALIGN.CENTER)

    # --- High-Engagement Sales (highlight mode; the T+ visit subset) ---
    if he_on and chart_images.get('he_funnel'):
        t = he['threshold']
        sh = new_slide(f"High-Engagement Sales: {t}+ Campaign Visits")
        add_text(sh, Inches(0.5), Inches(1.25), Inches(12.3), Inches(0.45),
                 f"A conservative view of the {he['total_sold']:,} viewed vehicles sold: the "
                 f"{he['sold']:,} our audience came back to most often.", 13, GREY, italic=True)
        right = chart_images.get('he_ltb') or chart_images.get('he_mix')
        if right is not None:
            add_picture_fit(sh, chart_images['he_funnel'], 0.45, 1.85, 6.0, 4.18)
            add_picture_fit(sh, right, 6.85, 1.85, 6.0, 4.18)
        else:
            add_picture_fit(sh, chart_images['he_funnel'], 3.65, 1.85, 6.0, 4.18)
        for i, (big, small) in enumerate(he_stat_chips(he)):
            x = 0.45 + i * 4.18
            chip = fill_rect(sh, Inches(x), Inches(6.12), Inches(4.0), Inches(0.72), LIGHT)
            try:
                chip.adjustments[0] = 0.15
            except Exception:
                pass
            add_text(sh, Inches(x + 0.15), Inches(6.12), Inches(3.7), Inches(0.40), big, 15, NAVY, bold=True)
            add_text(sh, Inches(x + 0.15), Inches(6.45), Inches(3.7), Inches(0.35), small, 9.5, GREY)

    # --- Slide 3: Top Sold ---
    if sold_df is not None and not sold_df.empty:
        s3 = new_slide("Top Sold: What Our Audience Viewed That Sold")
        top_models = _model_counts(sold_df, 'Units Sold')
        if not top_models.empty:
            add_text(s3, Inches(0.5), Inches(1.35), Inches(4.6), Inches(0.4),
                     "Top Sold Models (>1 Unit)", 14, NAVY, bold=True)
            rows = [["Make/Model", "Units"]] + [
                [str(r['Make/Model'])[:30], str(r['Units Sold'])]
                for _, r in top_models.iterrows()]
            add_table(s3, Inches(0.5), Inches(1.8), Inches(4.4), rows,
                      [4, 1], font_size=10)
        detail_x = 5.3 if not top_models.empty else 0.5
        detail_w = 7.5 if not top_models.empty else 12.3
        add_text(s3, Inches(detail_x), Inches(1.35), Inches(detail_w), Inches(0.4),
                 "Top Sold Units (Most Campaign Visits)", 14, NAVY, bold=True)
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        rows = [["Vehicle", "Type", "Visitors", "VIN"]] + [
            [str(r['Vehicle Name'])[:34], str(r['Type']),
             str(r['Attributed Unique Visitors']), str(r['VIN'])]
            for _, r in top_sold.iterrows()]
        add_table(s3, Inches(detail_x), Inches(1.8), Inches(detail_w), rows,
                  [3.4, 0.9, 1.0, 2.4], font_size=9)

    # --- Slide 4: Missed Opportunities ---
    if has_missed:
        s4 = new_slide("Missed Opportunities: The Watch List")
        add_text(s4, Inches(0.5), Inches(1.3), Inches(12.3), Inches(0.6),
                 "Active vehicles receiving above-average campaign traffic that "
                 "haven't sold yet - review these VDPs for missing photos, "
                 "\"Call for Price\" buttons, or pricing outliers.", 12, GREY,
                 italic=True)
        top_missed = missed_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        rows = [["Vehicle", "Type", "Visitors", "VIN"]] + [
            [str(r['Vehicle Name'])[:40], str(r['Type']),
             str(r['Attributed Unique Visitors']), str(r['VIN'])]
            for _, r in top_missed.iterrows()]
        add_table(s4, Inches(0.5), Inches(2.0), Inches(12.3), rows,
                  [4.2, 0.9, 1.0, 2.4], font_size=10)

    # --- Auto Group performance charts (multi-dealer) ---
    if has_group and (chart_images.get('group_traffic') or chart_images.get('group_sales')):
        sgc = new_slide("Auto Group Performance")
        if chart_images.get('group_traffic') and chart_images.get('group_sales'):
            add_picture_fit(sgc, chart_images['group_traffic'], 0.45, 1.5, 6.1, 4.7)
            add_picture_fit(sgc, chart_images['group_sales'], 6.8, 1.5, 6.1, 4.7)
        else:
            only = chart_images.get('group_traffic') or chart_images.get('group_sales')
            add_picture_fit(sgc, only, 3.6, 1.5, 6.1, 4.7)
        add_text(sgc, Inches(0.5), Inches(6.45), Inches(12.3), Inches(0.5),
                 "Where the campaign drove traffic across the group - and which "
                 "stores converted it into inventory movement.", 12, GREY,
                 italic=True, align=PP_ALIGN.CENTER)

    # --- Auto Group scoreboard (multi-dealer) ---
    if has_group:
        group_rows = dealer_group.reset_index(drop=True)
        per_slide = 12
        n_pages = (len(group_rows) + per_slide - 1) // per_slide
        for page in range(n_pages):
            title = "Auto Group Scoreboard"
            if n_pages > 1:
                title += f" ({page + 1} of {n_pages})"
            sg = new_slide(title)
            chunk = group_rows.iloc[page * per_slide:(page + 1) * per_slide]
            he_col = he_on and 'High-Engagement Sold' in chunk.columns
            rows = [["Dealer", "Traffic", "VDPs", "Sold"]
                    + ([f"{he['threshold']}+ Visit Sold"] if he_col else [])
                    + ["LTB", "Est. Value Sold", "Est. Value Viewed"]]
            for _, r in chunk.iterrows():
                rows.append([str(r['Dealer'])[:32], str(r['Total Visitors']),
                             str(r['VDPs Shopped']), str(r['Units Sold'])]
                            + ([str(int(r['High-Engagement Sold']))] if he_col else [])
                            + [f"{r['Look-to-Book (%)']}%",
                               _fmt_money(r['Est. Rev Sold']),
                               _fmt_money(r['Pipeline Value'])])
            fracs = ([3.4, 1.0, 0.8, 0.8] + ([1.1] if he_col else [])
                     + [0.9, 1.5, 1.5])
            add_table(sg, Inches(0.5), Inches(1.5), Inches(12.3), rows,
                      fracs, font_size=10)

    # --- Dealer Deep Dives (one slide per dealer) ---
    if include_dealer_details and has_group:
        for _, grow in dealer_group.iterrows():
            dealer = grow['Dealer']
            d_sold = sold_df[sold_df['Dealer'] == dealer] if sold_df is not None and not sold_df.empty else pd.DataFrame()
            d_missed = missed_df[missed_df['Dealer'] == dealer] if not missed_df.empty else pd.DataFrame()
            if d_sold.empty and d_missed.empty:
                continue
            sd = new_slide(f"Dealer Profile: {str(dealer)[:48]}")
            add_text(sd, Inches(0.5), Inches(1.25), Inches(12.3), Inches(0.4),
                     f"Traffic: {grow['Total Visitors']}   |   VDPs Viewed: {grow['VDPs Shopped']}   |   "
                     f"Units Sold: {grow['Units Sold']}   |   LTB: {grow['Look-to-Book (%)']}%   |   "
                     f"Est. Value Sold: {_fmt_money(grow['Est. Rev Sold'])}   |   "
                     f"Est. Value Viewed: {_fmt_money(grow['Pipeline Value'])}",
                     12, NAVY, bold=True)
            # Row 1: aggregated model roll-ups (mirrors the dashboard + PDF).
            d_sold_models = _model_counts(d_sold, 'Units Sold') if not d_sold.empty else pd.DataFrame()
            d_missed_models = _model_counts(d_missed, 'Missed Count') if not d_missed.empty else pd.DataFrame()
            agg_bottom = 1.85
            if not d_sold_models.empty:
                add_text(sd, Inches(0.5), Inches(1.72), Inches(5.9), Inches(0.35),
                         "Top Sold Models (>1 Unit)", 12, NAVY, bold=True)
                rows = [["Make/Model", "Units"]] + [
                    [str(r['Make/Model'])[:32], str(r['Units Sold'])]
                    for _, r in d_sold_models.head(5).iterrows()]
                add_table(sd, Inches(0.5), Inches(2.08), Inches(5.9), rows,
                          [4.6, 1.3], font_size=9, row_h_in=0.27)
                agg_bottom = max(agg_bottom, 2.08 + 0.27 * len(rows))
            if not d_missed_models.empty:
                add_text(sd, Inches(6.9), Inches(1.72), Inches(5.9), Inches(0.35),
                         "Top Missed Models (>1 Unit)", 12, NAVY, bold=True)
                rows = [["Make/Model", "Missed"]] + [
                    [str(r['Make/Model'])[:32], str(r['Missed Count'])]
                    for _, r in d_missed_models.head(5).iterrows()]
                add_table(sd, Inches(6.9), Inches(2.08), Inches(5.9), rows,
                          [4.6, 1.3], font_size=9, row_h_in=0.27)
                agg_bottom = max(agg_bottom, 2.08 + 0.27 * len(rows))

            # Row 2: vehicle-level detail (up to 8 rows, space permitting).
            det_y = agg_bottom + 0.28
            det_rows = 8 if det_y < 4.2 else 6
            if not d_sold.empty:
                add_text(sd, Inches(0.5), Inches(det_y), Inches(5.9), Inches(0.35),
                         "Top Sold Units", 12, NAVY, bold=True)
                d_top = d_sold.sort_values('Attributed Unique Visitors', ascending=False).head(det_rows)
                rows = [["Vehicle", "Type", "Visits", "VIN"]] + [
                    [str(r['Vehicle Name'])[:24], str(r['Type']),
                     str(r['Attributed Unique Visitors']), str(r['VIN'])]
                    for _, r in d_top.iterrows()]
                add_table(sd, Inches(0.5), Inches(det_y + 0.36), Inches(5.9), rows,
                          [2.3, 0.7, 0.7, 2.2], font_size=8, row_h_in=0.26)
            if not d_missed.empty:
                add_text(sd, Inches(6.9), Inches(det_y), Inches(5.9), Inches(0.35),
                         "Missed Opportunities (Watch List)", 12, NAVY, bold=True)
                d_topm = d_missed.sort_values('Attributed Unique Visitors', ascending=False).head(det_rows)
                rows = [["Vehicle", "Type", "Visits", "VIN"]] + [
                    [str(r['Vehicle Name'])[:24], str(r['Type']),
                     str(r['Attributed Unique Visitors']), str(r['VIN'])]
                    for _, r in d_topm.iterrows()]
                add_table(sd, Inches(6.9), Inches(det_y + 0.36), Inches(5.9), rows,
                          [2.3, 0.7, 0.7, 2.2], font_size=8, row_h_in=0.26)

    # --- Glossary & Methodology ---
    sg2 = new_slide("Glossary & Methodology")
    glossary = client_glossary(he if he_on else None)
    tb = sg2.shapes.add_textbox(Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.2))
    tf = tb.text_frame; tf.word_wrap = True
    first = True
    from pptx.util import Pt as _Pt
    for head, body in glossary:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        r = p.add_run(); r.text = head
        r.font.name = "Arial"; r.font.size = _Pt(14); r.font.bold = True
        r.font.color.rgb = NAVY
        p2 = tf.add_paragraph()
        r2 = p2.add_run(); r2.text = body
        r2.font.name = "Arial"; r2.font.size = _Pt(11); r2.font.color.rgb = DARK
        p2.space_after = _Pt(8)

    out = io.BytesIO()
    prs.save(out); out.seek(0)
    return out.read()

# --- PDF GENERATOR FUNCTION ---
def create_pdf_report(df, sold_df, metrics, missed_df, include_missed, dealer_group=None, include_dealer_details=False, chart_images=None, report_title=None):
    """
    Client PDF. Section order and titles mirror the PowerPoint deck so the two
    read as the same report: Executive Summary, Traffic Mix, What Sold,
    High-Engagement Sales (highlight mode), Top Sold, Missed Opportunities,
    Auto Group Performance, Dealer Profiles, Glossary & Methodology.
    """
    import tempfile
    try:
        from PIL import Image as PILImage
    except Exception:
        PILImage = None

    # HELPER: Sanitizes scraped text so it doesn't crash the PDF encoder
    def safe_str(text):
        if pd.isna(text): return ""
        text = str(text)
        reps = {'“':'"', '”':'"', '‘':"'", '’':"'", '—':'-', '–':'-', '®':'', '™':'', '©':''}
        for k, v in reps.items(): text = text.replace(k, v)
        return text.encode('latin-1', 'ignore').decode('latin-1')

    # HELPER: navy header row for tables (sleeker look, matches the KPI band)
    def thead(cols, size=9):
        pdf.set_font("Arial", "B", size)
        pdf.set_fill_color(10, 31, 92)
        pdf.set_text_color(255, 255, 255)
        for w, t in cols:
            pdf.cell(w, 8, t, border=1, fill=True)
        pdf.ln()
        pdf.set_text_color(0, 0, 0)
        pdf.set_fill_color(240, 240, 240)

    sec_no = [0]
    def section(title, new_page=False, numbered=True, need=70):
        """Grey section bar; never orphaned at the bottom of a page (needs
        room for the bar plus `need` mm of what follows)."""
        if new_page or pdf.get_y() + 14 + need > pdf.h - pdf.b_margin:
            pdf.add_page()
        if numbered:
            sec_no[0] += 1
            title = f"{sec_no[0]}. {title}"
        pdf.set_font("Arial", "B", 14)
        pdf.set_fill_color(240, 240, 240)
        pdf.set_text_color(10, 31, 92)
        pdf.cell(0, 10, " " + safe_str(title), ln=True, fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

    def subhead(title):
        if pdf.get_y() + 26 > pdf.h - pdf.b_margin:
            pdf.add_page()
        pdf.set_font("Arial", "B", 11)
        pdf.set_text_color(10, 31, 92)
        pdf.cell(0, 8, safe_str(title), ln=True)
        pdf.set_text_color(0, 0, 0)

    def caption(text, align="C"):
        pdf.set_font("Arial", "I", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(0, 4.5, safe_str(text), align=align)
        pdf.set_text_color(0, 0, 0)

    def _save(buf):
        if buf is None:
            return None
        try:
            buf.seek(0)
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            tf.write(buf.read()); tf.close()
            buf.seek(0)
            return tf.name
        except Exception:
            return None

    def _img_h_mm(path, w_mm):
        if PILImage is not None:
            try:
                with PILImage.open(path) as im:
                    return w_mm * im.height / im.width
            except Exception:
                pass
        return w_mm * 0.62  # safe fallback aspect

    def _ensure_space(h_mm):
        if pdf.get_y() + h_mm > pdf.h - pdf.b_margin - 2:
            pdf.add_page()

    def image_pair(left_buf, right_buf, w=88):
        """Two charts side by side (or one centred); heights measured, so they
        can never run off the page."""
        paths = [p for p in (_save(left_buf), _save(right_buf)) if p]
        if not paths:
            return
        row_h = max(_img_h_mm(p, w) for p in paths)
        _ensure_space(row_h + 6)
        y = pdf.get_y()
        xs = (15, 107) if len(paths) == 2 else ((210 - w) / 2,)
        for p, x in zip(paths, xs):
            pdf.image(p, x=x, y=y, w=w)
        pdf.set_y(y + row_h + 1)
        for p in paths:
            try: os.remove(p)
            except Exception: pass

    def fit_table(n_rows, extra=0):
        """Keep a table (header + rows) on one page when it fits on one."""
        h = 8 * (n_rows + 1) + extra
        if h < pdf.h - pdf.t_margin - pdf.b_margin - 4:
            _ensure_space(h)

    def vehicle_table(frame, clickable=False, header=None):
        fit_table(len(frame), extra=8 if header else 0)
        if header:
            subhead(header)
        thead([(85, "Vehicle" + (" (click to open)" if clickable else "")), (25, "Type"),
               (20, "Visitors"), (60, "VIN")], size=10)
        for _, row in frame.iterrows():
            pdf.set_font("Arial", "", 9)
            if clickable:
                pdf.set_text_color(0, 0, 255)
                pdf.cell(85, 8, safe_str(row['Vehicle Name'])[:38], border=1, link=str(row['Page Url']))
                pdf.set_text_color(0, 0, 0)
            else:
                pdf.cell(85, 8, safe_str(row['Vehicle Name'])[:38], border=1)
            pdf.cell(25, 8, safe_str(row['Type']), border=1)
            pdf.cell(20, 8, str(row['Attributed Unique Visitors']), border=1)
            pdf.set_font("Arial", "", 8)
            pdf.cell(60, 8, safe_str(row['VIN']), border=1)
            pdf.ln()

    def model_table(frame, count_label, header, top_n=10):
        f = frame.copy()
        f['Model_Only'] = f['Vehicle Name'].apply(normalize_model)
        counts = f['Model_Only'].value_counts().reset_index()
        counts.columns = ['Make/Model', 'Count']
        counts = counts[counts['Count'] > 1].head(top_n)
        if counts.empty:
            return False
        fit_table(len(counts), extra=8)
        subhead(header)
        thead([(100, "Make/Model"), (40, count_label)], size=10)
        pdf.set_font("Arial", "", 9)
        for _, row in counts.iterrows():
            pdf.cell(100, 8, safe_str(row['Make/Model']), border=1)
            pdf.cell(40, 8, str(row['Count']), border=1)
            pdf.ln()
        pdf.ln(4)
        return True

    def mix_table(series, label):
        fit_table(len(series))
        thead([(70, label), (35, "Sold Count"), (35, "Share %")])
        pdf.set_font("Arial", "", 9)
        total = float(series.sum()) or 1.0
        for k, v in series.items():
            pdf.cell(70, 8, safe_str(k), border=1)
            pdf.cell(35, 8, str(int(v)), border=1)
            pdf.cell(35, 8, f"{v / total * 100:.1f}%", border=1)
            pdf.ln()

    chart_images = chart_images or {}
    _ctx = (metrics or {}).get('context') or {}
    he = (metrics or {}).get('high_engagement')
    he_on = bool(he and he.get('export_ok'))
    has_group = dealer_group is not None and not dealer_group.empty
    missed_df = missed_df if missed_df is not None else pd.DataFrame()

    pdf = FPDF()
    pdf.add_page()

    eastern = pytz.timezone('US/Eastern')
    current_time = datetime.datetime.now(eastern)

    # --- Title block (same wording as slide 1 of the deck) ---
    if report_title:
        display_title = clean_report_title(report_title)
    elif df['Dealer'].nunique() == 1:
        display_title = str(df['Dealer'].iloc[0])
    else:
        display_title = "Auto Group"
    pdf.set_font("Arial", "B", 18)
    pdf.set_text_color(10, 31, 92)
    pdf.cell(0, 11, "Premion Website Attribution", ln=True, align="C")
    pdf.set_font("Arial", "B", 13)
    pdf.cell(0, 8, safe_str(f"Inventory Impact Report  |  {display_title}"), ln=True, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "I", 10)
    pdf.cell(0, 7, safe_str(_ctx.get('header_meta')
                            or f"Generated {current_time.strftime('%B %d, %Y').replace(' 0', ' ')}"),
             ln=True, align="C")
    pdf.ln(4)

    # --- 1. Executive Summary ---
    section("Executive Summary")
    caption("Complementary to your website attribution: the vehicles our audience viewed on "
            "the dealer's site - and how quickly that inventory is moving.")
    pdf.ln(2)
    # Navy dashboard-style KPI band; falls back to plain text if imaging fails.
    band_done = False
    band_path = _save(build_kpi_band_image(metrics))
    if band_path:
        try:
            pdf.image(band_path, x=15, w=180)
            band_done = True
            pdf.ln(2)
        except Exception:
            band_done = False
        try: os.remove(band_path)
        except Exception: pass
    if not band_done:
        pdf.set_font("Arial", "", 12)
        col_width = pdf.w / 2.2
        pdf.cell(col_width, 8, f"Viewed Vehicles Sold: {metrics['units_sold']}", border=0)
        pdf.cell(col_width, 8, f"Est. Value Sold: ${metrics['rev_sold']:,.0f}", border=0, ln=True)
        pdf.cell(col_width, 8, f"Est. Total Value Viewed: ${metrics['pipeline']:,.0f}", border=0)
        pdf.cell(col_width, 8, f"Look-to-Book Ratio: {metrics['ltb']}%", border=0, ln=True)
        _chip = ltb_chip_text(metrics)
        if _chip:
            pdf.set_font("Arial", "I", 10)
            pdf.cell(col_width, 6, "", border=0)
            pdf.cell(col_width, 6, safe_str(f"({_chip})"), border=0, ln=True)
    if _ctx.get('threshold_line'):
        pdf.set_font("Arial", "", 9)
        pdf.set_text_color(10, 31, 92)
        pdf.cell(0, 5, safe_str(_ctx['threshold_line']), ln=True, align="C")
        pdf.set_text_color(0, 0, 0)
    if he_on:
        # Secondary, lighter strip under the navy band (mirrors slide 1).
        pdf.ln(2)
        y0 = pdf.get_y()
        pdf.set_fill_color(239, 241, 250)
        pdf.rect(15, y0, 180, 17, style="F")
        pdf.set_fill_color(10, 31, 92)
        pdf.rect(15, y0, 1.6, 17, style="F")
        pdf.set_xy(19, y0 + 2.2)
        pdf.set_font("Arial", "B", 9)
        pdf.set_text_color(10, 31, 92)
        pdf.cell(52, 5, "HIGH-ENGAGEMENT SALES")
        pdf.set_xy(19, y0 + 8.5)
        pdf.set_font("Arial", "", 7.5)
        pdf.set_text_color(107, 114, 128)
        pdf.cell(52, 5, safe_str(f"Vehicles with {he['threshold']}+ campaign visits"))
        cols = [(f"{he['sold']:,}", f"of the {he['total_sold']:,} sold", 28),
                (f"${he['value_sold']:,.0f}", "est. value sold", 36)]
        _third = he_third_stat(he)
        if _third:
            cols.append((_third[0], _third[2], 52))
        x = 74
        for big, small, w in cols:
            pdf.set_xy(x, y0 + 1.5)
            pdf.set_font("Arial", "B", 13)
            pdf.set_text_color(10, 31, 92)
            pdf.cell(w, 7, safe_str(big))
            pdf.set_xy(x, y0 + 8.8)
            pdf.set_font("Arial", "", 7.5)
            pdf.set_text_color(107, 114, 128)
            pdf.cell(w, 5, safe_str(small))
            x += w + 2
        pdf.set_text_color(0, 0, 0)
        pdf.set_y(y0 + 19)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, "Look-to-Book = viewed vehicles sold / all viewed vehicles - how quickly the inventory our audience engaged with is moving.", ln=True, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    # --- 2. Traffic Mix: Where the Audience Shopped ---
    section("Traffic Mix: Where the Audience Shopped")
    if _ctx.get('unique_visitors_fmt') and _ctx.get('avg_pages'):
        # Engagement stats as figures, not prose.
        pdf.set_fill_color(10, 31, 92)
        pdf.set_text_color(255, 255, 255)
        pdf.set_x(15)
        pdf.set_font("Arial", "B", 8)
        pdf.cell(89, 6, "UNIQUE VISITORS", border=0, fill=True, align="C")
        pdf.cell(2, 6, "", border=0)
        pdf.cell(89, 6, "AVG. PAGES PER VISITOR", border=0, fill=True, align="C")
        pdf.ln(6)
        pdf.set_x(15)
        pdf.set_font("Arial", "B", 15)
        pdf.cell(89, 9, safe_str(_ctx['unique_visitors_fmt']), border=0, fill=True, align="C")
        pdf.cell(2, 9, "", border=0)
        pdf.cell(89, 9, safe_str(_ctx['avg_pages']), border=0, fill=True, align="C")
        pdf.ln(12)
        pdf.set_text_color(0, 0, 0)
        pdf.set_fill_color(240, 240, 240)
    tpath = _save(chart_images.get('traffic'))
    if tpath:
        try:
            _ensure_space(_img_h_mm(tpath, 180) + 8)
            pdf.image(tpath, x=15, w=180)
        except Exception:
            pass
        try: os.remove(tpath)
        except Exception: pass
    caption("VDP visits are high-intent inventory shoppers - the audience most likely to buy.")
    pdf.ln(3)
    traffic_mix = df.groupby('Category')['Attributed Unique Visitors'].sum().reset_index().sort_values('Attributed Unique Visitors', ascending=False)
    fit_table(len(traffic_mix), extra=8)
    subhead("Traffic Mix (Unique Visits by Page Type)")
    thead([(100, "Page Category"), (40, "Unique Visits")])
    pdf.set_font("Arial", "", 9)
    for _, row in traffic_mix.iterrows():
        pdf.cell(100, 8, safe_str(row['Category']), border=1)
        pdf.cell(40, 8, str(row['Attributed Unique Visitors']), border=1)
        pdf.ln()
    pdf.ln(6)

    # --- 3. What Sold: Sales Mix & Value Tiers ---
    if sold_df is not None and not sold_df.empty:
        section("What Sold: Sales Mix & Value Tiers")
        image_pair(chart_images.get('salesmix'), chart_images.get('tiers'))
        caption("The mix of inventory our traffic helped move - what sold, and at what price points.")
        pdf.ln(3)
        fit_table(sold_df['Type'].nunique(), extra=8)
        subhead("Sales Mix (New vs Used)")
        mix_table(sold_df['Type'].value_counts(), "Type")
        pdf.ln(4)
        fit_table(sold_df['Price Tier'].nunique(), extra=8)
        subhead("Sold Value Tiers")
        tiers = sold_df['Price Tier'].value_counts()
        tiers = tiers.reindex([t for t in TIER_ORDER if t in tiers.index]
                              + [t for t in tiers.index if t not in TIER_ORDER])
        mix_table(tiers, "Price Tier")
        pdf.ln(6)

    # --- 4. High-Engagement Sales (highlight mode) ---
    if he_on and chart_images.get('he_funnel'):
        section(f"High-Engagement Sales: {he['threshold']}+ Campaign Visits")
        caption(f"A conservative view of the {he['total_sold']:,} viewed vehicles sold: the "
                f"{he['sold']:,} our audience came back to most often.")
        pdf.ln(2)
        image_pair(chart_images.get('he_funnel'),
                   chart_images.get('he_ltb') or chart_images.get('he_mix'))
        chips = he_stat_chips(he)
        if chips:
            _ensure_space(16)
            y0 = pdf.get_y() + 1
            w = (180 - 4 * (len(chips) - 1)) / len(chips)
            for i, (big, small) in enumerate(chips):
                x = 15 + i * (w + 4)
                pdf.set_fill_color(239, 241, 250)
                pdf.rect(x, y0, w, 14, style="F")
                pdf.set_xy(x + 3, y0 + 1.5)
                pdf.set_font("Arial", "B", 11)
                pdf.set_text_color(10, 31, 92)
                pdf.cell(w - 6, 6, safe_str(big))
                pdf.set_xy(x + 3, y0 + 7.5)
                pdf.set_font("Arial", "", 7)
                pdf.set_text_color(107, 114, 128)
                pdf.cell(w - 6, 5, safe_str(small))
            pdf.set_text_color(0, 0, 0)
            pdf.set_y(y0 + 18)
        pdf.ln(4)

    # --- 5. Top Sold: What Our Audience Viewed That Sold ---
    section("Top Sold: What Our Audience Viewed That Sold", need=100)
    if sold_df is not None and not sold_df.empty:
        model_table(sold_df, "Units Sold", "Top Sold Models (>1 Unit)")
        vehicle_table(sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10),
                      header="Top Sold Units (Most Campaign Visits)")
    else:
        pdf.set_font("Arial", "", 9)
        pdf.cell(0, 8, "No sales identified.", border=1, ln=True)
    pdf.ln(6)

    # --- 6. Missed Opportunities: The Watch List ---
    if include_missed:
        section("Missed Opportunities: The Watch List", new_page=True)
        caption("Active vehicles receiving above-average campaign traffic that haven't sold yet - "
                "review these VDPs for missing photos, \"Call for Price\" buttons, or pricing outliers.",
                align="L")
        pdf.ln(3)
        if not missed_df.empty:
            model_table(missed_df, "Missed Count", "Top Missed Models (>1 Unit)")
            vehicle_table(missed_df.sort_values('Attributed Unique Visitors', ascending=False).head(10),
                          clickable=True, header="Watch List Vehicles")
        else:
            pdf.set_font("Arial", "", 9)
            pdf.cell(0, 8, "No missed opportunities identified.", border=1, ln=True)
        pdf.ln(6)

    # --- 7. Auto Group Performance (multi-dealer) ---
    if has_group:
        section("Auto Group Performance", new_page=True)
        if chart_images.get('group_traffic') or chart_images.get('group_sales'):
            image_pair(chart_images.get('group_traffic'), chart_images.get('group_sales'))
            caption("Where the campaign drove traffic across the group - and which stores "
                    "converted it into inventory movement.")
            pdf.ln(3)
        fit_table(min(len(dealer_group), 15), extra=8)
        subhead("Auto Group Scoreboard")
        he_col = he_on and 'High-Engagement Sold' in dealer_group.columns
        cols = [(50 if he_col else 52, "Dealer"), (18, "Traffic"), (15, "VDPs"), (15, "Sold")]
        if he_col:
            cols.append((18, f"{he['threshold']}+ Visit"))
        cols += [(15, "LTB"), (29 if he_col else 35, "Est. Value Sold"),
                 (29 if he_col else 35, "Est. Value Viewed")]
        thead(cols, size=8)
        for _, row in dealer_group.head(15).iterrows():
            _dn = safe_str(row['Dealer'])
            pdf.set_font("Arial", "", 7 if len(_dn) > 28 else 8)
            pdf.cell(cols[0][0], 8, _dn[:40], border=1)
            pdf.set_font("Arial", "", 8)
            vals = [str(row['Total Visitors']), str(row['VDPs Shopped']), str(row['Units Sold'])]
            if he_col:
                vals.append(str(int(row['High-Engagement Sold'])))
            vals += [f"{row['Look-to-Book (%)']}%", f"${row['Est. Rev Sold']:,.0f}",
                     f"${row['Pipeline Value']:,.0f}"]
            for (w, _h), v in zip(cols[1:], vals):
                pdf.cell(w, 8, v, border=1)
            pdf.ln()
        pdf.ln(6)

    # --- Dealer Profiles (one page per dealer) ---
    if include_dealer_details and has_group:
        for dealer in dealer_group['Dealer'].tolist():
            dealer_sold = sold_df[sold_df['Dealer'] == dealer] if sold_df is not None and not sold_df.empty else pd.DataFrame()
            dealer_missed = missed_df[missed_df['Dealer'] == dealer] if not missed_df.empty else pd.DataFrame()
            if dealer_sold.empty and dealer_missed.empty:
                continue
            section(f"Dealer Profile: {dealer}", new_page=True, numbered=False)
            if not dealer_sold.empty:
                model_table(dealer_sold, "Units Sold", "Top Sold Models (>1 Unit)", top_n=5)
            if not dealer_missed.empty:
                model_table(dealer_missed, "Missed Count", "Top Missed Models (>1 Unit)", top_n=5)
            if not dealer_sold.empty:
                vehicle_table(dealer_sold.sort_values('Attributed Unique Visitors', ascending=False).head(10),
                              header="Top Sold Units")
                pdf.ln(5)
            if not dealer_missed.empty:
                vehicle_table(dealer_missed.sort_values('Attributed Unique Visitors', ascending=False).head(10),
                              clickable=True, header="Missed Opportunities (Watch List)")

    # --- GLOSSARY & METHODOLOGY ---
    section("Glossary & Methodology", new_page=True, numbered=False)
    for g_head, g_body in client_glossary(he if he_on else None):
        pdf.set_font("Arial", "B", 10)
        pdf.set_text_color(10, 31, 92)
        pdf.cell(0, 7, safe_str(g_head), ln=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Arial", "", 9)
        pdf.multi_cell(0, 5, safe_str(g_body))
        pdf.ln(2)

    pdf_out = pdf.output(dest='S')
    return pdf_out.encode('latin-1') if isinstance(pdf_out, str) else bytes(pdf_out)

# --- THE VALUATION ENGINE ---
def estimate_value(row):
    name = str(row['Vehicle Name']).lower()
    vehicle_type = str(row['Type']).lower()
    MODEL_PRICES = {
        'f-150': 60000, 'f 150': 60000, 'f150': 60000, 'super duty': 80000,
        'ranger': 40000, 'maverick': 28000, 'bronco': 50000, 'explorer': 48000,
        'expedition': 70000, 'edge': 40000, 'escape': 32000, 'mustang': 45000,
        'mach-e': 55000, 'silverado': 55000, 'sierra': 62000, 'colorado': 38000,
        'tahoe': 72000, 'suburban': 78000, 'yukon': 78000, 'escalade': 110000,
        'corvette': 90000, 'camaro': 42000, 'blazer': 40000, 'equinox': 32000,
        'traverse': 42000, 'malibu': 28000, 'grand cherokee': 55000, 'wagoneer': 80000,
        'wrangler': 48000, 'gladiator': 50000, 'ram 1500': 60000, 'durango': 50000,
        'charger': 40000, 'challenger': 45000, 'pacifica': 45000, 'compass': 30000,
        'tundra': 58000, 'tacoma': 42000, '4runner': 50000, 'sequoia': 75000,
        'highlander': 45000, 'rav4': 35000, 'camry': 30000, 'corolla': 25000,
        'prius': 32000, 'sienna': 48000, 'pilot': 48000, 'passport': 42000,
        'cr-v': 36000, 'odyssey': 45000, 'civic': 28000, 'accord': 32000,
        '3 series': 50000, '5 series': 65000, 'x3': 55000, 'x5': 75000, 'x7': 90000,
        'c-class': 52000, 'e-class': 70000, 'gle': 75000, 'gls': 95000,
        'rx': 58000, 'nx': 48000, 'es': 48000, 'gx': 70000, 'lx': 100000,
        'gv80': 70000, 'gv70': 55000, 'telluride': 48000, 'palisade': 50000,
        'lyriq': 65000, 'optiq': 54000, 'ct4': 40000, 'ct5': 50000, 'xt4': 40000, 'xt5': 50000
    }
    BRAND_DEFAULTS = {
        'cadillac': 65000, 'mercedes': 70000, 'bmw': 65000, 'audi': 60000,
        'lexus': 60000, 'lincoln': 65000, 'land rover': 85000, 'porsche': 100000,
        'ford': 45000, 'chevrolet': 40000, 'gmc': 55000, 'ram': 55000,
        'jeep': 45000, 'dodge': 40000, 'toyota': 38000, 'honda': 35000,
        'nissan': 32000, 'hyundai': 30000, 'kia': 30000, 'subaru': 32000,
        'volkswagen': 32000, 'mazda': 32000, 'volvo': 55000
    }
    baseline = 35000
    found_model = False
    sorted_models = sorted(MODEL_PRICES.keys(), key=len, reverse=True)
    for model in sorted_models:
        if re.search(r'\b' + re.escape(model) + r'\b', name):
            baseline = MODEL_PRICES[model]
            found_model = True
            break
    if not found_model:
        for brand, price in BRAND_DEFAULTS.items():
            if brand in name:
                baseline = price
                break
    if 'new' in vehicle_type:
        return int(baseline)
    
    eastern = pytz.timezone('US/Eastern')
    year_match = re.search(r'\d{4}', name)
    year = int(year_match.group(0)) if year_match else 2025
    current_year = datetime.datetime.now(eastern).year + 1
    age = current_year - year
    if age <= 0:
        value = baseline
    else:
        value = baseline * 0.85
        for _ in range(age - 1):
            value = value * 0.90
    return int(value)

def get_price_tier(price):
    if price < 30000: return "Budget (<$30k)"
    if price < 60000: return "Core ($30k-$60k)"
    return "Premium ($60k+)"

# --- THE SCANNING HELPERS ---
def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

# ISO 3779 VIN check-digit transliteration (position 9 validates the whole VIN).
_VIN_TRANS = {**{str(d): d for d in range(10)},
              **dict(zip("ABCDEFGHJKLMNPRSTUVWXYZ",
                         [1, 2, 3, 4, 5, 6, 7, 8, 1, 2, 3, 4, 5, 7, 9, 2, 3, 4, 5, 6, 7, 8, 9]))}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

def vin_check_digit_ok(vin):
    """True if the VIN satisfies the North American check digit (position 9)."""
    try:
        vin = str(vin).upper()
        if len(vin) != 17 or re.search(r'[IOQ]', vin):
            return False
        total = sum(_VIN_TRANS[c] * _VIN_WEIGHTS[i] for i, c in enumerate(vin))
        rem = total % 11
        return vin[8] == ("X" if rem == 10 else str(rem))
    except Exception:
        return False

def is_plausible_vin(candidate):
    """
    Guards against 17-char slices of hex page IDs (some dealer platforms use
    32-char GUIDs in VDP URLs, e.g. .../2024-Acura-MDX-Macon-0b4e...b630.htm).
    Such a slice looks VIN-shaped but is not a VIN, and treating it as one
    makes every vehicle look SOLD. Accept only if the candidate passes the
    check digit, or contains a letter that hex IDs cannot have (G-Z except
    I/O/Q). Real VINs satisfy at least one of these; GUID slices satisfy
    neither.
    """
    c = str(candidate).upper()
    if len(c) != 17 or re.search(r'[IOQ]', c) or not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', c):
        return False
    if vin_check_digit_ok(c):
        return True
    return bool(re.search(r'[G-HJ-NP-Z]', c))   # non-hex letter => not a hex ID

def extract_vin(url):
    try:
        path = urlparse(str(url)).path.upper().strip('/')
        match = re.search(r'(?:^|[-/])([A-HJ-NPR-Z0-9]{17})(?:[-/\.]|$)', path)
        if match and is_plausible_vin(match.group(1)):
            return match.group(1)
        # Fallback: only standalone 17-char tokens, and only if they look like
        # real VINs. A slice out of a longer alphanumeric run (page GUID) is
        # NOT a VIN — grabbing it would cause false "SOLD" results, so we
        # return N/A and let the year-based logic handle it instead.
        for token in re.split(r'[^A-HJ-NPR-Z0-9]+', path):
            if len(token) == 17 and is_plausible_vin(token):
                return token
        return "N/A"
    except:
        return "N/A"

def extract_type(url):
    u = str(url).lower()
    if 'used' in u and 'new' not in u: return 'Used'
    if 'new' in u and 'used' not in u: return 'New'
    if '/used' in u or '-used-' in u or '=used' in u or 'preowned' in u: return 'Used'
    if '/new' in u or '-new-' in u or '=new' in u: return 'New'
    return 'New' if re.search(r'202[5-7]', u) else 'Used'

def smart_dealer_name(url, is_multi=False):
    u = str(url).lower()
    if not u.startswith('http'):
        u = 'http://' + u
    netloc = urlparse(u).netloc
    netloc = re.sub(r'^(www\.|shop\.|inventory\.|cars\.)', '', netloc)
    s = netloc.split('.')[0]
    
    brands = ['honda', 'toyota', 'ford', 'chevrolet', 'chevy', 'nissan', 'jeep', 'chrysler', 'dodge', 'ram',
              'hyundai', 'kia', 'vw', 'volkswagen', 'bmw', 'mercedes', 'audi', 'lexus', 'acura',
              'infiniti', 'subaru', 'mazda', 'volvo', 'porsche', 'buick', 'gmc', 'cadillac', 'lincoln', 'mitsubishi']
    
    modifiers = ['of', 'auto', 'group', 'cars', 'motors', 'dealers', 'cares', 'center', 'city', 'town', 'resale', 'classic', 'one', 'mile', 'superstore']
    
    for mod in modifiers:
        s = s.replace(mod, f" {mod} ")
        
    brands.sort(key=len, reverse=True)
    has_brand = False
    
    for brand in brands:
        if brand in s:
            if brand == 'ram' and ('paramus' in s or 'framingham' in s):
                continue
            if brand == 'ford' and ('oxford' in s or 'crawford' in s or 'stratford' in s):
                continue
            s = s.replace(brand, f" {brand} ")
            has_brand = True
            
    s = re.sub(r'\s+', ' ', s).strip().title()
    s = s.replace(" Vw", " VW").replace(" Bmw", " BMW").replace(" Gmc", " GMC")
    
    if is_multi and not has_brand:
        if any(x in s for x in ["Cares", "Community", "Gives"]):
            s += " (Community)"
        elif any(x in s for x in ["Resale", "Classic", "Used", "Preowned"]):
            pass 
        else:
            s += " (Group/Central Site)"
            
    return s

def _dealer_domain(url):
    u = str(url).lower()
    if not u.startswith('http'):
        u = 'http://' + u
    return re.sub(r'^(www\.)', '', urlparse(u).netloc)

_BRAND_WORDS = ['honda', 'toyota', 'ford', 'chevrolet', 'chevy', 'nissan', 'jeep', 'chrysler',
                'dodge', 'ram', 'hyundai', 'kia', 'vw', 'volkswagen', 'bmw', 'mercedes', 'audi',
                'lexus', 'acura', 'infiniti', 'subaru', 'mazda', 'volvo', 'porsche', 'buick', 'gmc',
                'cadillac', 'lincoln', 'mitsubishi', 'genesis', 'land rover', 'jaguar', 'mini']

def is_group_site_url(url, is_multi=False):
    """
    True when this site is a group/central site rather than a single rooftop.
    Order of evidence:
      1. the "Is Group Site" column in the DealerNames tab, if filled in;
      2. the dealer's real name (sheet or its own website): a brand in the
         name means a rooftop ("Ted Britt Ford of Chantilly"); "Group" or
         "Dealerships" means a group site; any other named store (a truck or
         collision center) is a rooftop;
      3. only with no name at all, the old guess from the domain.
    """
    dom = _dealer_domain(url)
    flags = st.session_state.get("dealer_group_flags") or {}
    if dom in flags:
        return bool(flags[dom])
    names = st.session_state.get("dealer_display_names") or {}
    nm = names.get(dom) or HARVESTED_DEALER_NAMES.get(dom)
    if nm:
        low = nm.lower()
        if any(re.search(r'\b' + re.escape(b) + r'\b', low) for b in _BRAND_WORDS):
            return False
        return bool(re.search(r'\b(group|dealerships|auto group|automotive group)\b', low))
    return "(Group/Central Site)" in smart_dealer_name(url, is_multi)

def dealer_display_name(url, is_multi=False):
    """
    Client-facing dealer name. Uses the DealerNames sheet tab when the domain is
    listed (spelled the way the client spells it); otherwise falls back to the
    name derived from the domain, WITHOUT the "(Group/Central Site)" marker —
    that's carried separately as is_group_site.
    """
    names = st.session_state.get("dealer_display_names") or {}
    dom = _dealer_domain(url)
    keys = (dom, re.sub(r'^(shop\.|inventory\.|cars\.)', '', dom))
    for key in keys:                       # 1. DealerNames sheet override
        if key in names:
            return names[key]
    for key in keys:                       # 2. the dealer's own site
        if key in HARVESTED_DEALER_NAMES:
            return HARVESTED_DEALER_NAMES[key]
    # 3. derived from the domain, without the group-site marker
    return smart_dealer_name(url, is_multi).replace(" (Group/Central Site)", "").strip()

def harvest_names_for_unnamed(urls, session, timeout=8):
    """
    Dealers with traffic but no vehicle pages (a truck or service center) never
    had a page fetched during the scan, so there's nothing to read a name from.
    Fetch their homepage once. Skips any dealer already named by the sheet or
    the scan, so normal dealers cost nothing extra.
    """
    names = st.session_state.get("dealer_display_names") or {}
    seen = set()
    for u in urls:
        dom = _dealer_domain(u)
        if not dom or dom in seen or dom in names or dom in HARVESTED_DEALER_NAMES:
            continue
        seen.add(dom)
        try:
            r = session.get(f"https://{dom}/", timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"})
            if r.status_code == 200 and r.text:
                nm = harvest_dealer_name_from_html(r.text, BeautifulSoup(r.text, 'html.parser'), dom)
                if nm:
                    HARVESTED_DEALER_NAMES[dom] = nm
        except Exception:
            continue

def resolve_dealer_names(urls, is_multi=False):
    """
    domain -> display name for a run. If two different domains resolve to the
    same name (e.g. a group site and a rooftop both self-describe as the
    group), those domains fall back to their domain-derived names so the
    stores are never merged into one row.
    """
    by_dom = {}
    for u in urls:
        d = _dealer_domain(u)
        if d and d not in by_dom:
            by_dom[d] = (u, dealer_display_name(u, is_multi))
    counts = {}
    for _d, (_u, n) in by_dom.items():
        counts[n] = counts.get(n, 0) + 1
    out = {}
    for d, (u, n) in by_dom.items():
        if counts[n] > 1:
            n = smart_dealer_name(u, is_multi).replace(" (Group/Central Site)", "").strip()
        out[d] = n
    return out

def clean_name_universal(url):
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    
    parsed = urlparse(str(url))
    path_str = parsed.path + (("?" + parsed.query) if parsed.query else "")
    
    brands = ['Jeep', 'Ford', 'Gmc', 'Toyota', 'Dodge', 'Ram', 'Chrysler', 'Chevrolet', 'Honda', 'Nissan', 'Hyundai', 'Kia', 'Bmw', 'Lexus', 'Volvo', 'Volkswagen', 'Subaru', 'Mazda', 'Mercedes', 'Audi', 'Cadillac', 'Buick', 'Acura', 'Infiniti', 'Lincoln', 'Land Rover', 'Jaguar', 'Porsche', 'Mini']
    make = ""
    for b in brands:
        if b.lower() in path_str.lower():
            make = b.title()
            break
            
    parts = path_str.split(str(year), 1)
    rest = parts[-1] if len(parts) > 1 else path_str
    
    rest = rest.replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    tokens = rest.split()
    
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory', 'Parts', 'Service', 'Finance', 'Global', 'Incentives', 'Offers', 'Suv', 'Truck', 'Coupe', 'Sedan', 'Vehicle', 'Vehicles']
    clean_tokens = [t for t in tokens if not (len(t) > 10 and any(c.isdigit() for c in t)) and t.title() not in junk and t.title() != make]
    
    clean_tokens = [t for t in clean_tokens if t != str(year)]
    
    return f"{year} {make} {' '.join(clean_tokens)}".title().strip()

def categorize(u):
    u = str(u).lower()
    
    if any(x in u for x in ['thank', 'confirm', 'success']):
        return 'Online Conversions'
        
    if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
    if any(x in u for x in ['service', 'parts', 'collision', 'appointment', 'maintenance']): return 'Service'
    
    if any(x in u for x in ['incentive', 'offers', 'specials', 'promotions', 'rebate']): 
        return 'Incentives/Offers'
        
    if 'index.htm' in u or u.endswith('searchnew.aspx') or u.endswith('searchused.aspx') or u.endswith('searchall.aspx'):
        if 'new' in u: return 'New Car Search'
        if 'used' in u or 'preowned' in u: return 'Used Car Search'
        return 'General Search'
        
    if get_year(u): return 'VDP'
    
    if any(x in u for x in ['search', 'inventory', 'vehicles']):
        if 'new' in u: return 'New Car Search'
        if 'used' in u or 'preowned' in u: return 'Used Car Search'
        return 'General Search'
        
    return 'Other'

from collections import defaultdict

# ======================================================================
# DEALER INSPIRE BATCHED VIN RESOLVER
# Checking availability one Algolia query per VIN means thousands of HTTP calls
# (which ground the Basil group report to a halt). Algolia's multi-query endpoint
# lets us pack ~50 VIN lookups into ONE request, and each lookup only needs the
# match count (hitsPerPage=0, no car data), so the payload is tiny. This turns
# ~10,000 requests into a couple hundred and works for a store of any size —
# no 1,000-record pagination ceiling. Runs on algolia.net, never the firewall.
# ======================================================================

# Real VINs discovered during the scan (for dealers whose URLs don't carry one).
HARVESTED_VINS = {}

def url_key(url):
    """
    The identifying token from a VDP link, for dealers whose URLs carry a page
    ID instead of a VIN (e.g. .../2024-Acura-MDX-Macon-0b4e4a42ac18...b630.htm
    -> '0b4e4a42ac184abdfb3c758d6119b630'). Falls back to the last path slug.
    """
    try:
        path = urlparse(str(url)).path.rstrip('/')
        slug = re.sub(r'\.html?$', '', path.split('/')[-1])
        m = re.findall(r'[0-9a-fA-F]{16,}', slug)
        if m:
            return m[-1]
        return slug
    except Exception:
        return ""

def resolve_algolia_keys(domain, cfg, keys, session):
    """
    Resolve VIN-less vehicles against the dealer's own Algolia index using the
    page ID from each VDP link. Returns (status_map, vin_map).
    If EVERY key comes back with no hits, the index likely doesn't search that
    field — we return nothing so the caller falls through to the MarketCheck
    URL match rather than declaring live cars sold.
    """
    app_id, api_key, index_name = cfg['app_id'], cfg['api_key'], cfg['index']
    endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "x-algolia-application-id": app_id,
        "x-algolia-api-key": api_key,
        "Content-Type": "application/json",
        "Referer": f"https://www.{domain}/",
        "Origin": f"https://www.{domain}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    status, vins_found = {}, {}
    keys = [k for k in keys if k]
    BATCH = 50
    try:
        for i in range(0, len(keys), BATCH):
            chunk = keys[i:i + BATCH]
            body = {"requests": [
                {"indexName": index_name, "params": f"query={k}&hitsPerPage=1"}
                for k in chunk
            ]}
            r = session.post(endpoint, headers=headers, json=body, timeout=15)
            if r.status_code != 200:
                break
            for k, res in zip(chunk, r.json().get("results", [])):
                hits = res.get("hits", []) or []
                if res.get("nbHits", 0) > 0:
                    status[k] = "Available"
                    for h in hits[:1]:
                        for f in ("vin", "VIN", "objectID"):
                            cand = str(h.get(f, "")).upper().strip()
                            if is_plausible_vin(cand):
                                vins_found[k] = cand
                                break
                else:
                    status[k] = "SOLD (Not in Dealer Database)"
    except Exception:
        pass
    # Nothing matched at all -> the page ID isn't searchable in this index.
    if status and not any(v == "Available" for v in status.values()):
        return ({}, {})
    return (status, vins_found)

def resolve_algolia_vins(domain, cfg, vins, session):
    """
    Resolve a list of VINs for one store via batched Algolia multi-queries.
    Returns {vin: "Available" | "SOLD (Not in Dealer Database)"} for whatever
    resolved; VINs left out (on failure) fall back to the per-VIN path in scan_url.
    """
    app_id, api_key, index_name = cfg['app_id'], cfg['api_key'], cfg['index']
    endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "x-algolia-application-id": app_id,
        "x-algolia-api-key": api_key,
        "Content-Type": "application/json",
        "Referer": f"https://www.{domain}/",
        "Origin": f"https://www.{domain}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    out = {}
    vins = list(vins)
    BATCH = 50
    try:
        for i in range(0, len(vins), BATCH):
            chunk = vins[i:i + BATCH]
            body = {"requests": [
                {"indexName": index_name, "params": f"query={v}&hitsPerPage=0"}
                for v in chunk
            ]}
            r = session.post(endpoint, headers=headers, json=body, timeout=15)
            if r.status_code != 200:
                break  # leave the rest unresolved -> scan_url per-VIN fallback
            results = r.json().get("results", [])
            for v, res in zip(chunk, results):
                nb = res.get("nbHits", 0)
                out[v] = "Available" if nb > 0 else "SOLD (Not in Dealer Database)"
    except Exception:
        pass
    return out

# ======================================================================
# MARKETCHECK EXTERNAL FALLBACK
# For dealers that are locked (API rejected + firewall-blocked) so neither our
# own API nor the scraper can reach them, MarketCheck's daily-crawled inventory
# answers "is this VIN still listed?". Only fires for cars the normal scan left
# as errors, and only if a key is set in Streamlit secrets.
# ======================================================================

def _find_secret(obj, name, depth=0):
    """Recursively search a secrets mapping for a key by name."""
    if depth > 4:
        return ""
    try:
        v = obj.get(name) if hasattr(obj, "get") else None
        if v:
            return str(v).strip()
    except Exception:
        pass
    try:
        for k in obj:
            sub = obj[k]
            if hasattr(sub, "get") or hasattr(sub, "__getitem__"):
                found = _find_secret(sub, name, depth + 1)
                if found:
                    return found
    except Exception:
        pass
    return ""

def get_marketcheck_key():
    """
    Read the MarketCheck key from Streamlit secrets. Prefers the top level (the
    correct place) but searches nested sections too, so a key mistakenly placed
    under a header like [connections.gsheets] is still found. '' if missing.
    """
    try:
        return _find_secret(st.secrets, "MARKETCHECK_KEY")
    except Exception:
        return ""

# Sold-detection endpoint. CONFIRMED BY MARKETCHECK (Jul 2026):
#  - /v2/dealerships/inventory = "Dealer Inventory Syndication API": $1.00/call
#    on PAID tiers, but NO DATA FEES on the FREE tier (10 listings/page there).
#  - /v2/search/car/active = "Inventory Search API" ($0.002/call): officially
#    meant for zip/lat-long searches; source= filtering works in isolation but
#    is unsupported and flaky mid-run (per-VIN vin= fallback covers it).
# FREE-TIER CONFIG (the $0/month setup): set in settings —
#   MARKETCHECK_ENDPOINT = "https://api.marketcheck.com/v2/dealerships/inventory"
#   MARKETCHECK_PAGE_SIZE = 10, MARKETCHECK_PAGE_CAP = 500,
#   MARKETCHECK_BUDGET = 75, MARKETCHECK_MONTHLY_CAP = 450
# On PAID tiers keep the default below (search endpoint, pennies per run).
_MC_DEFAULT_ENDPOINT = "https://api.marketcheck.com/v2/search/car/active"

def get_admin_key():
    """Admin unlock key from settings (ADMIN_KEY). Empty if not configured."""
    try:
        return str(_find_secret(st.secrets, "ADMIN_KEY") or "").strip()
    except Exception:
        return ""

def is_admin():
    """
    Admin-only panels (lookup status, diagnostic, data export) stay hidden
    from reps. Unlock for a session via the sidebar Admin box, or by opening
    the app with ?admin=<ADMIN_KEY> in the URL.
    """
    if st.session_state.get("_is_admin"):
        return True
    key = get_admin_key()
    if not key:
        return False
    try:
        qp = st.query_params.get("admin")
        if isinstance(qp, list):
            qp = qp[0] if qp else None
        if qp and str(qp) == key:
            st.session_state["_is_admin"] = True
            return True
    except Exception:
        pass
    return False

def _mc_endpoint():
    try:
        v = str(_find_secret(st.secrets, "MARKETCHECK_ENDPOINT")).strip()
        return v if v.startswith("http") else _MC_DEFAULT_ENDPOINT
    except Exception:
        return _MC_DEFAULT_ENDPOINT
MC_ENDPOINT = _mc_endpoint()

def _mc_setting_int(name, default):
    """Read an integer app setting from secrets (any nesting); default if absent."""
    try:
        v = _find_secret(st.secrets, name)
        return int(str(v).strip()) if str(v).strip() else default
    except Exception:
        return default

# Pagination ceiling per inventory slice. Free plan = 500 rows. After upgrading
# to Basic, set MARKETCHECK_PAGE_CAP = 1500 in settings and every pull gets
# cheaper (most dealers fit in one un-sliced pass).
MC_PAGE_CAP = _mc_setting_int("MARKETCHECK_PAGE_CAP", 500)

# Hard spending ceiling per report. A single run can never use more market
# lookups than this, no matter what. Override with MARKETCHECK_BUDGET in
# settings (suggested: 75 on the free plan, 150 on Basic).
MC_BUDGET = _mc_setting_int("MARKETCHECK_BUDGET", 75)

# Optional month-wide ceiling across ALL reports (0 = disabled). For the free
# plan set MARKETCHECK_MONTHLY_CAP = 450 in settings: runs stop spending when
# the month's total (from the UsageStats log) reaches the cap, keeping a
# reserve under the plan's 500-call allowance.
MC_MONTH_CAP = _mc_setting_int("MARKETCHECK_MONTHLY_CAP", 0)

# Dealer-inventory memory: complete MarketCheck store pulls are remembered in
# the Google Sheet ("MCCache" tab) and reused for this many days, so repeat
# reports on the same blocked dealer cost ZERO lookups. 0 disables.
MC_CACHE_DAYS = _mc_setting_int("MARKETCHECK_CACHE_DAYS", 7)

# Auto-continue: when the per-pass budget stops a run mid-dealer, automatically
# start another budgeted pass on the still-unresolved vehicles (max 4 passes per
# run, halts immediately on zero progress, and the monthly cap always wins).
# 1 = on (default), 0 = off (single-pass behavior).
MC_AUTO_CONTINUE = _mc_setting_int("MARKETCHECK_AUTO_CONTINUE", 1)

# Market lookups are for BLOCKED sites, not stragglers: a dealer only routes to
# MarketCheck when at least this many of its vehicles couldn't be checked
# directly. Healthy dealers with a few scan hiccups are left as-is.
MC_MIN_UNRESOLVED = _mc_setting_int("MARKETCHECK_MIN_UNRESOLVED", 10)

# Cars fetched per lookup. Free plan serves small pages regardless of what's
# requested, so the safe default is 50. Basic honors larger pages — set
# MARKETCHECK_PAGE_SIZE = 200 in settings and big dealers cost ~1/4 the calls.
MC_PAGE_SIZE = _mc_setting_int("MARKETCHECK_PAGE_SIZE", 50)

class _MCStop(Exception):
    """Raised by the governor to halt the market-lookup phase immediately."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason   # "budget" | "limit" | "errors"

class _MCGovernor:
    """
    Gatekeeper for every market lookup. Enforces the per-run budget, paces
    requests under the plan's rate limit, and halts the phase instantly on
    quota/auth refusals or repeated failures — so a run can never hang and can
    never drain the monthly allowance.
    """
    def __init__(self, session, budget, month_used=0, month_cap=0):
        self._s = session
        self.budget = budget
        self.month_used = int(month_used or 0)
        self.month_cap = int(month_cap or 0)
        self.count = 0
        self.stopped = None        # None, or the _MCStop reason
        self.last_error = None     # human-readable detail of the last failure
        self._fail_streak = 0

    def get(self, *a, **k):
        if self.stopped:
            raise _MCStop(self.stopped)
        if self.count >= self.budget:
            self.stopped = "budget"
            raise _MCStop("budget")
        if self.month_cap and (self.month_used + self.count) >= self.month_cap:
            self.stopped = "monthly"
            raise _MCStop("monthly")
        time.sleep(0.22)           # pace safely under 5 requests/second
        self.count += 1
        try:
            r = self._s.get(*a, **k)
        except _MCStop:
            raise
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            self._fail_streak += 1
            if self._fail_streak >= 3:
                self.stopped = "errors"
                raise _MCStop("errors")
            raise
        if r.status_code in (401, 403, 429):
            # Key rejected / quota exhausted / rate refusal: stop the whole
            # phase now rather than burning calls into a wall.
            self.stopped = "limit"
            raise _MCStop("limit")
        if r.status_code != 200:
            try:
                body = str(r.text)[:200]
            except Exception:
                body = ""
            self.last_error = f"HTTP {r.status_code}: {body}"
            self._fail_streak += 1
            if self._fail_streak >= 3:
                self.stopped = "errors"
                raise _MCStop("errors")
        else:
            self._fail_streak = 0
        return r

def _mc_cache_load(conn):
    """Read the MCCache tab -> {domain: {active, absent, complete, age}}. Best-effort."""
    out = {}
    try:
        df = conn.read(worksheet="MCCache", ttl=0)
        now = datetime.datetime.now(datetime.timezone.utc)
        for _, row in df.iterrows():
            dom = str(row.get("Domain", "")).strip().lower()
            try:
                ts = pd.to_datetime(str(row.get("Timestamp", "")), utc=True)
                age = (now - ts.to_pydatetime()).total_seconds() / 86400.0
            except Exception:
                continue
            def _vset(cell):
                return {v for v in (x.strip().upper() for x in str(cell or "").split(","))
                        if len(v) == 17}
            active = _vset(row.get("ActiveVINs", ""))
            absent = _vset(row.get("AbsentVINs", ""))
            complete = str(row.get("Complete", "")).strip().upper() in ("TRUE", "1", "YES")
            if dom and (active or absent) and age >= 0:
                out[dom] = {"active": active, "absent": absent,
                            "complete": complete, "age": age}
    except Exception:
        pass
    return out

def _mc_cache_save(conn, updates, max_age_days=7):
    """
    Upsert {domain: {active, absent, complete}} into MCCache. A partial (per-VIN)
    result merges into a fresh existing row instead of replacing it; a complete
    store snapshot replaces the row and restarts its clock. Never raises.
    """
    if not updates:
        return
    try:
        cols = ["Domain", "ActiveVINs", "AbsentVINs", "Complete", "Count", "Timestamp"]
        try:
            df = conn.read(worksheet="MCCache", ttl=0)
            if df is None or not isinstance(df, pd.DataFrame):
                df = pd.DataFrame(columns=cols)
        except Exception:
            df = pd.DataFrame(columns=cols)
        existing = _mc_cache_load(conn) if len(df) else {}
        rows = {}
        for _, r in df.iterrows():
            d = str(r.get("Domain", "")).strip().lower()
            if d:
                rows[d] = {c: r.get(c, "") for c in cols}
        now = str(datetime.datetime.now(datetime.timezone.utc))
        for dom, upd in updates.items():
            active = set(upd.get("active") or set())
            absent = set(upd.get("absent") or set())
            complete = bool(upd.get("complete"))
            prev = existing.get(dom)
            ts = now
            if prev and prev["age"] <= max_age_days and not complete:
                # Merge partial knowledge into the fresh existing row; keep its
                # clock so staleness is anchored to the oldest information.
                active |= prev["active"]
                absent |= prev["absent"] - active
                complete = prev["complete"]
                old_row = rows.get(dom, {})
                ts = old_row.get("Timestamp", now) or now
            if complete:
                absent = set()   # a complete snapshot implies absence for the rest
            if not active and not absent:
                continue         # nothing learned: never write an empty row
            if len(active) + len(absent) > 2500:   # keep cells under Sheets limits
                continue
            rows[dom] = {"Domain": dom,
                         "ActiveVINs": ",".join(sorted(active)),
                         "AbsentVINs": ",".join(sorted(absent)),
                         "Complete": "TRUE" if complete else "FALSE",
                         "Count": len(active) + len(absent),
                         "Timestamp": ts}
        data = pd.DataFrame(list(rows.values()), columns=cols)
        try:
            conn.update(worksheet="MCCache", data=data)
        except Exception:
            conn.create(worksheet="MCCache", data=data)   # tab doesn't exist yet
    except Exception:
        pass

def _mc_request(domain, api_key, session, year=None, year_range=None, car_type=None, price_range=None, rows=50, start=0):
    """One MarketCheck dealer-inventory request. Returns (num_found, listings) or (None, None)."""
    on_search = (MC_ENDPOINT == _MC_DEFAULT_ENDPOINT)
    if on_search:
        # The Inventory Search API hard-caps rows at 50 and 422s on more —
        # clamp here so a MARKETCHECK_PAGE_SIZE=200 setting (fine on the old
        # syndication endpoint) can't poison every request.
        try:
            rows = min(int(rows), 50)
        except Exception:
            rows = 50
    params = {"api_key": api_key, "source": domain, "rows": str(rows), "start": str(start)}
    if year:
        params["year"] = str(year)
    if year_range:
        translated = False
        if on_search:
            # The search endpoint has no year_range param; send a comma list
            # of years instead (equivalent filter, universally supported).
            try:
                lo, hi = (int(x) for x in str(year_range).split("-"))
                if 0 <= hi - lo <= 30:
                    params["year"] = ",".join(str(y) for y in range(lo, hi + 1))
                    translated = True
            except Exception:
                pass
        if not translated:
            params["year_range"] = year_range
    if car_type:
        params["car_type"] = car_type
    if price_range:
        params["price_range"] = price_range
    try:
        r = session.get(MC_ENDPOINT, params=params, timeout=25)
        if r.status_code != 200:
            return (None, None)
        data = r.json()
        if "num_found" not in data:
            if hasattr(session, "last_error"):
                session.last_error = f"200 OK but unexpected body: {str(data)[:140]}"
            return (None, None)
        return (data.get("num_found", 0), data.get("listings", []))
    except _MCStop:
        raise   # governor halt: must reach the orchestrator, never swallowed
    except Exception as e:
        if hasattr(session, "last_error"):
            session.last_error = f"response error: {type(e).__name__}: {str(e)[:140]}"
        return (None, None)

def normalize_vdp_url(url):
    """
    Canonical form of a VDP link for matching our report URLs against the
    dealer's live listings: host without www, lowercase path, no query string,
    no trailing slash or .htm/.html suffix.
    """
    try:
        p = urlparse(str(url).strip().lower())
        host = p.netloc.replace("www.", "")
        path = re.sub(r'\.html?$', '', p.path.rstrip('/'))
        if not host and not path:
            return ""
        return f"{host}{path}"
    except Exception:
        return ""

def _mc_pull(domain, api_key, session, year=None, year_range=None, car_type=None, price_range=None):
    """
    Pull a slice's VINs (and the dealer VDP URLs on those listings), paging in
    MC_PAGE_SIZE steps. Aborts early (no paging) if the slice is over the
    free-tier cap, so an over-cap slice costs ONE call, not ten.
    Returns (vin_set, complete, num_found, url_set): complete means we got the
    whole slice (so an absent vehicle is genuinely sold). url_set lets us match
    dealers whose VDP links carry page IDs instead of VINs.
    """
    def _harvest(listings, vins, urls):
        """urls is a {normalized_vdp_url: vin} map so a URL match can also
        recover the real VIN for dealers whose links don't carry one."""
        for lst in listings:
            v = str(lst.get("vin", "")).upper().strip()
            if v:
                vins.add(v)
            for key in ("vdp_url", "vdp_url_full", "url"):
                u = lst.get(key)
                if u:
                    n = normalize_vdp_url(u)
                    if n:
                        urls[n] = v if is_plausible_vin(v) else urls.get(n)
                    break

    nf, listings = _mc_request(domain, api_key, session, year=year, year_range=year_range,
                               car_type=car_type, price_range=price_range, rows=MC_PAGE_SIZE, start=0)
    if nf is None:
        return (set(), False, None, {})
    vins, urls = set(), {}
    _harvest(listings, vins, urls)
    if nf > MC_PAGE_CAP:
        return (vins, False, nf, urls)   # over cap -> caller sub-slices
    start = len(listings)
    while start < nf and start < MC_PAGE_CAP:
        nf2, more = _mc_request(domain, api_key, session, year=year, year_range=year_range,
                                car_type=car_type, price_range=price_range, rows=MC_PAGE_SIZE, start=start)
        if nf2 is None or not more:
            break
        _harvest(more, vins, urls)
        start += len(more)
    return (vins, start >= nf, nf, urls)

def _mc_apply_by_url(out, items, url_set, min_match_rate=0.2):
    """
    Resolve vehicles by matching their VDP link against the dealer's live
    listings — for sites whose URLs carry page IDs instead of VINs.
    A match rate below min_match_rate means the two URL formats don't line up
    (MarketCheck may publish a different link style), so we resolve NOTHING
    rather than risk declaring live cars sold. Returns the match rate, or None
    if the attempt was rejected.
    """
    if not url_set or not items:
        return None
    # Match on the page ID rather than the whole path: these sites serve the
    # SAME vehicle under several slugs (.../2025-Honda-Pilot-Macon-<id> and
    # .../2025-Honda-Pilot-Macon-Georgia-<id>), and MarketCheck publishes only
    # one of them. Full-path matching marked the other variant sold.
    id_map = {}
    for n, v in (url_set.items() if isinstance(url_set, dict)
                 else ((x, None) for x in url_set)):
        k = url_key(n)
        if k:
            id_map[k] = v if v else id_map.get(k)
    normed = [(u, url_key(u), normalize_vdp_url(u)) for u, _v in items]
    hits = sum(1 for _u, k, n in normed
               if (k and k in id_map) or (n and n in url_set))
    rate = hits / max(len(normed), 1)
    if rate < min_match_rate:
        return None
    for u, k, n in normed:
        matched = (k and k in id_map) or (n and n in url_set)
        if matched:
            out[u] = "Available"
            # Recover the real VIN from the matched listing (these dealers'
            # links don't contain one).
            real = id_map.get(k) if k in id_map else (
                url_set.get(n) if isinstance(url_set, dict) else None)
            if real and is_plausible_vin(real):
                HARVESTED_VINS[k or url_key(u)] = real
        else:
            out[u] = "SOLD (Not in Market Inventory)"
    return rate

def _mc_apply(out, items, vin_set, complete):
    """Assign Available/Sold to report items from a pulled slice."""
    for url, vin in items:
        if vin.upper().strip() in vin_set:
            out[url] = "Available"
        elif complete:
            out[url] = "SOLD (Not in Market Inventory)"
        # partial slice -> leave the car's original result (no false sold)

def _mc_pull_priced(domain, api_key, session, year, car_type, lo, hi, depth=0, parent_nf=None):
    """
    Pull a car_type slice within a price band [lo, hi]. If that band is STILL over
    the cap, split it at the midpoint and recurse — so any distribution eventually
    fits. Stops splitting if a narrower band doesn't actually shrink the results
    (filter ignored by the data), so calls are never wasted on useless recursion.
    Returns (vin_set, complete).
    """
    vins, complete, nf, _u = _mc_pull(domain, api_key, session, year=year,
                                  car_type=car_type, price_range=f"{lo}-{hi}")
    if nf is None:
        return (set(), False)
    if complete:
        return (vins, True)
    if parent_nf is not None and nf >= parent_nf:
        return (vins, False)   # band didn't narrow anything -> splitting is futile
    if depth >= 4 or (hi - lo) <= 1000:
        return (vins, False)   # can't split further -> partial (never false-sold)
    mid = (lo + hi) // 2
    v1, c1 = _mc_pull_priced(domain, api_key, session, year, car_type, lo, mid, depth + 1, nf)
    v2, c2 = _mc_pull_priced(domain, api_key, session, year, car_type, mid, hi, depth + 1, nf)
    return (vins | v1 | v2, c1 and c2)

# Starting price bands for a car_type slice that's over the cap (tuned for autos;
# any band still too large is split further by _mc_pull_priced).
MC_PRICE_BANDS = [(0, 45000), (45000, 60000), (60000, 80000), (80000, 100000000)]

def _mc_resolve_year(domain, api_key, session, year, yr_items, out):
    """Resolve one model year; if over the cap, split by car_type, then by price."""
    vins, complete, nf, _u = _mc_pull(domain, api_key, session, year=year)
    if nf is None:
        return
    if complete:
        _mc_apply(out, yr_items, vins, True)
        return
    # Over cap -> split by car_type (each piece usually fits under the cap).
    merged = set(vins)
    all_complete = True
    for ct in ("new", "used", "certified"):
        vs, cp, nf2, _u2 = _mc_pull(domain, api_key, session, year=year, car_type=ct)
        if nf2 is None:
            all_complete = False
            continue
        if cp:
            merged |= vs
        else:
            # A single car_type still over the cap (e.g. 694 new units) -> split
            # it by price band, recursing on any band that's itself too large.
            for lo, hi in MC_PRICE_BANDS:
                pvs, pcp = _mc_pull_priced(domain, api_key, session, year, ct, lo, hi,
                                           parent_nf=nf2)
                merged |= pvs
                all_complete = all_complete and pcp
    _mc_apply(out, yr_items, merged, all_complete)

def _mc_band(domain, api_key, session, band_items, out):
    """
    Resolve a group of older report years as ONE year_range band (cheap when the
    years are sparse). If the band is over the cap, fall back to per-year.
    band_items = [(url, vin, year_int)].
    """
    if not band_items:
        return
    years = [y for _, _, y in band_items]
    lo, hi = min(years), max(years)
    vins, complete, nf, _u = _mc_pull(domain, api_key, session, year_range=f"{lo}-{hi}")
    if nf is None:
        return
    if complete:
        _mc_apply(out, [(u, v) for u, v, _ in band_items], vins, True)
        return
    # Band over the cap -> resolve each year on its own.
    per_year = defaultdict(list)
    for u, v, y in band_items:
        per_year[str(y)].append((u, v))
    for yr, its in per_year.items():
        _mc_resolve_year(domain, api_key, session, yr, its, out)

def _mc_vin_batch(chunk, api_key, session, domain=None):
    """
    One vins= batch lookup for [(url, vin)]. Returns ({url: status}, error).
    error is None on success; a string when the filter isn't honored or the
    call fails (caller decides whether to fall back).
    """
    params = {"api_key": api_key, "vins": ",".join(v for _, v in chunk),
              "rows": "50", "start": "0"}
    if domain:
        params["source"] = domain
    r = session.get(MC_ENDPOINT, params=params, timeout=25)
    if r.status_code != 200:
        try:
            body = str(r.text)[:160]
        except Exception:
            body = ""
        return ({}, f"vins= batch: HTTP {r.status_code}: {body}")
    data = r.json()
    nf = int(data.get("num_found", 0) or 0)
    listings = data.get("listings", [])
    # Same VIN can be listed by a few sources, but num_found beyond ~20x the
    # batch means the vins filter was ignored (national results). Refuse to
    # interpret that — never risk a false Sold.
    if nf > len(chunk) * 20:
        return ({}, f"vins= filter not honored (num_found={nf} for {len(chunk)} VINs)")
    active = set()
    for lst in listings:
        vv = str(lst.get("vin", "")).upper().strip()
        if vv:
            active.add(vv)
    start = len(listings)
    while start < nf and start < 400:
        r2 = session.get(MC_ENDPOINT, params={**params, "start": str(start)}, timeout=25)
        if r2.status_code != 200:
            break
        more = r2.json().get("listings", [])
        if not more:
            break
        for lst in more:
            vv = str(lst.get("vin", "")).upper().strip()
            if vv:
                active.add(vv)
        start += len(more)
    return ({u: ("Available" if v in active else "SOLD (Not in Market Inventory)")
             for u, v in chunk}, None)

def _mc_vin_single(url, vin, api_key, session, domain=None):
    """
    One vin= lookup, scoped to the dealer's domain. The free plan REQUIRES a
    source/dealer scope on this endpoint (a bare vin= is rejected with 400),
    and scoping is sharper anyway: "this VIN at this dealer".
    Returns (status_or_None, error). num_found for a single VIN must be tiny;
    a huge count means the filter is ignored -> error.
    """
    params = {"api_key": api_key, "vin": vin, "rows": "10", "start": "0"}
    if domain:
        params["source"] = domain
    r = session.get(MC_ENDPOINT, params=params, timeout=25)
    if r.status_code != 200:
        try:
            body = str(r.text)[:160]
        except Exception:
            body = ""
        return (None, f"vin= single: HTTP {r.status_code}: {body}")
    data = r.json()
    nf = int(data.get("num_found", 0) or 0)
    if nf > 50:
        return (None, f"vin= filter not honored (num_found={nf} for 1 VIN)")
    active = any(str(l.get("vin", "")).upper().strip() == vin
                 for l in data.get("listings", []))
    return ("Available" if active else "SOLD (Not in Market Inventory)", None)

def _mc_resolve_vins(domain, items, api_key, session, cache_out=None, out=None):
    """
    VIN-based resolution for the search endpoint: ask the market about the
    exact VINs in the report instead of pulling the dealer's whole store.
    Adaptive: tries a vins= batch first (~40 VINs per call); if that filter
    isn't honored by this account, falls back to per-VIN vin= lookups (one
    call each). If neither filter works, every failure detail is recorded
    and the dealer is left unresolved — never guessed.
    items = [(url, vin, year)]. Returns {url: status}. Pass a dict as `out`
    to keep partial results if a governor stop interrupts the loop mid-dealer.
    """
    out = {} if out is None else out
    valid = []
    for u, v, _y in items:
        vv = str(v or "").upper().strip()
        if is_plausible_vin(vv):     # never spend a lookup on a page-ID slice
            valid.append((u, vv))
    if not valid:
        return out
    CHUNK = 40
    # vins= is disabled: the 7/20 diagnostic returned num_found=211 for a
    # SINGLE VIN (wrong semantics — likely matching historical listings),
    # while vin= returned exactly 1. Per-VIN is production-proven; the batch
    # machinery is kept below in case MarketCheck fixes the param.
    mode = "single"
    batch_err = None
    for i in range(0, len(valid), CHUNK):
        chunk = valid[i:i + CHUNK]
        if mode in (None, "batch"):
            try:
                res, err = _mc_vin_batch(chunk, api_key, session, domain=domain)
            except _MCStop:
                raise
            except Exception as e:
                res, err = {}, f"vins= batch: {type(e).__name__}"
            if err is None:
                mode = "batch"
                out.update(res)
                continue
            if mode == "batch":
                # Batch worked earlier but failed now: transient — skip chunk.
                if hasattr(session, "last_error"):
                    session.last_error = err
                continue
            batch_err = err   # first chunk failed -> try per-VIN fallback
            mode = "single"
        # Per-VIN mode (fallback)
        for u, v in chunk:
            try:
                status, err = _mc_vin_single(u, v, api_key, session, domain=domain)
            except _MCStop:
                raise
            except Exception as e:
                status, err = None, f"vin= single: {type(e).__name__}"
            if err is not None:
                if hasattr(session, "last_error"):
                    session.last_error = (err if not batch_err
                                          else f"{batch_err} | then {err}")
                if "not honored" in err or "HTTP 4" in err:
                    break   # systemic: stop burning calls on this dealer
                continue
            out[u] = status
            # Remember each answer AS IT ARRIVES, so a budget/kill-switch stop
            # mid-dealer can never throw away knowledge already paid for.
            if cache_out is not None:
                entry = cache_out.setdefault(domain, {"active": set(),
                                                      "absent": set(),
                                                      "complete": False})
                if status == "Available":
                    entry["active"].add(v)
                elif str(status).startswith("SOLD"):
                    entry["absent"].add(v)
    return out

def resolve_dealer_marketcheck(domain, items, api_key, session, cache_out=None, out=None):
    """
    Resolve one locked dealer's unresolved cars against MarketCheck, minimizing
    calls. items = [(url, vin, year)]. Each slice is size-checked as it's pulled
    (aborting after one call if it's over the cap) so we never waste pages:
      • Small dealers (under the cap): the whole store in one pass.
      • Big dealers, tiered by age: newest years individually (dense; split by
        car_type if a single year is over the cap), older years bundled into bands.
    Returns {url: "Available" | "SOLD (Not in Market Inventory)"}.
    """
    out = {} if out is None else out
    # On the search endpoint: store-pull first (the 7/20 diagnostic confirmed
    # source= is honored there — Hamby returned num_found=75, dealer-sized),
    # which resolves a whole dealer in ceil(inventory/50) calls. The VIN path
    # (proven in production 7/20: exact match to baseline) is the fallback for
    # errors, unfiltered-looking responses, and whatever a partial pull left.
    if MC_ENDPOINT == _MC_DEFAULT_ENDPOINT:
        vins_s, complete_s, nf_s, urls_s = _mc_pull(domain, api_key, session)
        # Does this report even have VINs to match on? Some dealer platforms
        # publish page IDs instead (e.g. .../2024-Acura-MDX-Macon-0b4e...b630.htm).
        _usable_vins = sum(1 for _u, v, _y in items if is_plausible_vin(v))
        _vinless = _usable_vins < max(1, int(len(items) * 0.5))
        if nf_s is not None and 0 <= nf_s <= 25000:
            if complete_s:
                if _vinless:
                    # No VINs on our side: match on the dealer's own VDP links.
                    rate = _mc_apply_by_url(out, [(u, v) for u, v, _ in items], urls_s)
                    if rate is not None:
                        return out
                    if hasattr(session, "last_error"):
                        session.last_error = (
                            f"{domain}: no VINs in the report URLs and the live listing "
                            f"links didn't match either - left unresolved rather than "
                            f"guessing")
                    return out
                if cache_out is not None:
                    cache_out[domain] = {"active": set(vins_s), "absent": set(),
                                         "complete": True}
                _mc_apply(out, [(u, v) for u, v, _ in items], vins_s, True)
                return out
            # Partial pull (over the page cap): found VINs are safely
            # Available; resolve the remainder per-VIN.
            _mc_apply(out, [(u, v) for u, v, _ in items], vins_s, False)
            if cache_out is not None and vins_s:
                cache_out[domain] = {"active": set(vins_s), "absent": set(),
                                     "complete": False}
            remaining = [(u, v, y) for u, v, y in items if u not in out]
            _mc_resolve_vins(domain, remaining, api_key, session,
                             cache_out=cache_out, out=out)
            return out
        if nf_s is not None and nf_s > 25000 and hasattr(session, "last_error"):
            session.last_error = (f"source filter returned num_found={nf_s} for "
                                  f"{domain} — falling back to VIN lookups")
        _mc_resolve_vins(domain, items, api_key, session,
                         cache_out=cache_out, out=out)
        return out
    # Try the whole store; if it's over the cap this aborts after one call.
    vins, complete, nf, urls_s = _mc_pull(domain, api_key, session)
    if nf is None:
        return out
    if nf > 25000:
        # No dealership has 25k cars: the source filter isn't filtering.
        # Stop this dealer at ONE call instead of sub-slicing into a storm.
        if hasattr(session, "last_error"):
            session.last_error = (f"source filter not honored for {domain} "
                                  f"(num_found={nf}) — dealer left unresolved")
        return out
    if complete:
        _usable = sum(1 for _u, v, _y in items if is_plausible_vin(v))
        if _usable < max(1, int(len(items) * 0.5)):
            rate = _mc_apply_by_url(out, [(u, v) for u, v, _ in items], urls_s)
            if rate is None and hasattr(session, "last_error"):
                session.last_error = (f"{domain}: no VINs in the report URLs and the live "
                                      f"listing links didn't match either - left unresolved")
            return out
        if cache_out is not None:
            cache_out[domain] = {"active": set(vins), "absent": set(),
                                 "complete": True}
        _mc_apply(out, [(u, v) for u, v, _ in items], vins, True)
        return out

    # Big dealer: tier by vehicle age.
    try:
        current_year = datetime.datetime.now().year
    except Exception:
        current_year = max((int(y) for _, _, y in items if str(y).isdigit()), default=2025)
    recent_cut = current_year - 2   # newest 3 model years -> individual
    mid_cut = current_year - 9       # 4-9 years old -> mid band; older -> old band

    recent = defaultdict(list)   # "year" -> [(url, vin)]
    mid_items = []               # [(url, vin, year_int)]
    old_items = []
    for url, vin, year in items:
        if not (year and str(year).isdigit()):
            continue   # no reliable year on a >cap dealer -> leave unresolved (rare)
        y = int(year)
        if y >= recent_cut:
            recent[str(y)].append((url, vin))
        elif y >= mid_cut:
            mid_items.append((url, vin, y))
        else:
            old_items.append((url, vin, y))

    for year, yr_items in recent.items():
        _mc_resolve_year(domain, api_key, session, year, yr_items, out)
    _mc_band(domain, api_key, session, mid_items, out)
    _mc_band(domain, api_key, session, old_items, out)
    return out

def build_all_vin_status(vdp_urls, session):
    """
    Pre-pass: group the report's VINs by vaulted Dealer Inspire domain and resolve
    each store's VINs in batches (concurrently). Returns {domain: {vin: status}}.
    """
    by_domain = defaultdict(set)
    by_domain_keys = defaultdict(set)   # VIN-less vehicles, keyed by page ID
    for u in vdp_urls:
        d = urlparse(str(u)).netloc.replace('www.', '').lower()
        cfg = DEALER_API_VAULT.get(d)
        if isinstance(cfg, dict) and not cfg.get('ignore') \
           and cfg.get('app_id') and cfg.get('api_key') and cfg.get('index'):
            v = extract_vin(u)
            if v != "N/A":
                by_domain[d].add(v)
            else:
                by_domain_keys[d].add(url_key(u))
    status = {}
    for d, keys in by_domain_keys.items():
        try:
            kstat, kvins = resolve_algolia_keys(d, DEALER_API_VAULT[d], keys, session)
            if kstat:
                status.setdefault(d, {}).update(kstat)
                HARVESTED_VINS.update(kvins)
        except Exception:
            pass
    if not by_domain:
        return status
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(by_domain))) as ex:
        futs = {ex.submit(resolve_algolia_vins, d, DEALER_API_VAULT[d], vins, session): d
                for d, vins in by_domain.items()}
        for fut in concurrent.futures.as_completed(futs):
            d = futs[fut]
            try:
                status.setdefault(d, {}).update(fut.result())
            except Exception:
                status.setdefault(d, {})
    return status

def scan_url(url, session, ignored_domains, ignore_lock, vin_status=None):
    """
    Decide a VDP's status. Order of operations:
      1. HARD IGNORE: if the sheet flags the domain, or we've already been blocked
         by it this run, go straight to the visual scraper. No API call is built.
      2. If there are no usable credentials, use the scraper.
      3. Otherwise fire the Algolia API. On 200 -> read hits. On 401/403/429 (or any
         network error) -> blacklist the domain for the rest of this run and fall
         back to the scraper immediately. We never return a raw API ERROR for a
         blocked API — the scraper always gets a chance.
    """
    domain = urlparse(str(url)).netloc.replace('www.', '').lower()

    # --- 1. NON-DEALER-INSPIRE DEALERS -> SCRAPER ------------------------
    cfg = DEALER_API_VAULT.get(domain)
    sheet_says_ignore = isinstance(cfg, dict) and cfg.get('ignore') is True
    has_creds = (
        isinstance(cfg, dict)
        and cfg.get('app_id') and cfg.get('api_key') and cfg.get('index')
    )
    # Dealers marked ignore, or with no API creds, run on a scrapable platform.
    if sheet_says_ignore or not has_creds:
        return check_universal_status(url, session)

    # --- 2. DEALER INSPIRE (Algolia) DEALERS -----------------------------
    # These sites are firewall-protected, so scraping them is doomed — if the
    # API can't answer, we fail fast rather than dragging the report through a
    # blocked scrape.
    with ignore_lock:
        if domain in ignored_domains:          # this store's key already rejected
            return "ERROR (Inventory Unavailable)"

    vin = extract_vin(url)
    pre = (vin_status or {}).get(domain)
    if vin == "N/A":
        # No VIN in the link: the bulk pre-pass may have resolved this vehicle
        # by its page ID against the dealer's own index. If not, leave it for
        # the MarketCheck URL match rather than guessing.
        k = url_key(url)
        if pre and k in pre:
            return pre[k]
        # Nothing from the dealer's index: check the page itself. These sites
        # are usually publicly readable even when their API isn't useful, and a
        # live page (or a 404/redirect) is better evidence than any lookup.
        return check_universal_status(url, session)

    # Resolved in bulk by the pre-pass?
    if pre and vin in pre:
        return pre[vin]

    # --- 3. PER-VIN FALLBACK (only if the bulk pass missed this VIN) ------
    app_id, api_key, index_name = cfg['app_id'], cfg['api_key'], cfg['index']
    api_endpoint = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index_name}/query"
    api_headers = {
        "x-algolia-application-id": app_id,
        "x-algolia-api-key": api_key,
        "Content-Type": "application/json",
        "Referer": f"https://www.{domain}/",
        "Origin": f"https://www.{domain}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    }
    try:
        resp = session.post(api_endpoint, headers=api_headers,
                            json={"params": f"query={vin}"}, timeout=5)
    except Exception:
        return "ERROR (Inventory Unavailable)"

    if resp.status_code == 200:
        try:
            hits = resp.json().get("nbHits", 0)
        except Exception:
            return "ERROR (Inventory Unavailable)"
        return "Available" if hits > 0 else "SOLD (Not in Dealer Database)"

    # Key rejected / rate-limited -> blacklist this store so the rest fail fast.
    if resp.status_code in (401, 403, 429):
        with ignore_lock:
            ignored_domains.add(domain)
    return "ERROR (Inventory Unavailable)"

# --- THE UNIVERSAL VISUAL SCRAPER ---------------------------------------
# VIN markers on a VDP, most authoritative first. A page usually repeats its
# own vehicle's VIN in several of these; "similar vehicles" blocks mention
# other VINs once or twice, so frequency breaks ties in the fallback.
_VIN_PATTERNS = [
    r'vehicleIdentificationNumber["\']?\s*[:=]\s*["\']([A-HJ-NPR-Z0-9]{17})["\']',
    r'data-vin\s*=\s*["\']([A-HJ-NPR-Z0-9]{17})["\']',
    r'["\']vin["\']\s*:\s*["\']([A-HJ-NPR-Z0-9]{17})["\']',
    r'\bVIN\b[\s:#]*([A-HJ-NPR-Z0-9]{17})\b',
]

# Dealer names read off the dealers' own pages during the scan (domain -> name).
HARVESTED_DEALER_NAMES = {}

_GENERIC_NAMES = {"home", "homepage", "new inventory", "used inventory", "inventory",
                  "vehicle details", "dealer.com", "dealer inspire", "dealerinspire",
                  "cars.com", "dealeron", "page not found", "404", "search results"}
_DEALER_TYPES = {"autodealer", "automotivebusiness", "localbusiness", "organization",
                 "cardealer", "autorepair"}

def _name_matches_domain(name, domain):
    """A real dealer name shares a distinctive word with its own domain
    ("Ted Britt Ford of Chantilly" ~ tedbrittchantilly.com). Stops us picking up
    a platform name, an OEM name, or page furniture instead."""
    core = re.sub(r'[^a-z0-9]', '', domain.split('.')[0].lower())
    # 4+ letter words, plus route/highway numbers ("Ford of 495", "Route 22 Honda").
    words = [w for w in re.findall(r'[a-z0-9]+', name.lower())
             if len(w) >= 4 or (w.isdigit() and len(w) >= 2)]
    skip = {"ford", "chevrolet", "toyota", "honda", "nissan", "lincoln", "acura", "lexus",
            "subaru", "hyundai", "mazda", "dodge", "chrysler", "jeep", "cadillac", "buick",
            "motors", "auto", "automotive", "group", "cars", "sales", "dealer", "dealership"}
    return any(w in core for w in words if w not in skip)

def _clean_dealer_name(n):
    n = re.sub(r'\s+', ' ', str(n or '')).strip(' |-–—:')
    # Vehicle pages often append the VIN or a stock number to the dealer name in
    # the title ("Ted Britt Chevrolet 2GC4KME76T1206960"). Drop any token that
    # mixes letters and digits and is 6+ characters long — no real dealer name
    # has one ("Route 1 Toyota" and "Ford of 495" are unaffected).
    toks = [t for t in n.split(' ')
            if not (len(t) >= 6 and re.search(r'\d', t) and re.search(r'[A-Za-z]', t))]
    n = ' '.join(toks).strip(' |-–—:#')
    n = re.sub(r'\s*(?:stock|stk|vin)\s*#?\s*$', '', n, flags=re.IGNORECASE).strip(' |-–—:#')
    if n.isupper() and len(n) > 4:
        n = n.title()
    return n

def harvest_dealer_name_from_html(text, soup, domain):
    """
    The dealership's own name from its page, most authoritative source first:
    schema.org dealer/seller markup, then og:site_name, then the <title>.
    Returns a validated name or None — never a guess.
    """
    cands = []
    try:
        if soup is not None:
            for tag in soup.find_all('script', type='application/ld+json'):
                try:
                    blob = json.loads(tag.string or tag.get_text() or '')
                except Exception:
                    continue
                stack = blob if isinstance(blob, list) else [blob]
                while stack:
                    node = stack.pop()
                    if isinstance(node, list):
                        stack.extend(node); continue
                    if not isinstance(node, dict):
                        continue
                    t = node.get('@type')
                    types = {str(x).lower() for x in (t if isinstance(t, list) else [t])}
                    if types & _DEALER_TYPES and node.get('name'):
                        cands.append(node['name'])
                    for k in ('seller', 'offers', '@graph', 'provider', 'brand'):
                        if k in node:
                            stack.append(node[k])
            og = soup.find('meta', attrs={'property': 'og:site_name'})
            if og and og.get('content'):
                cands.append(og['content'])
            if soup.title and soup.title.string:
                for seg in re.split(r'\s[|\-–—:]\s', soup.title.string):
                    cands.append(seg)
    except Exception:
        pass
    # Pick the candidate that explains the MOST of the domain, not merely the
    # first one sharing a word with it: in "Commercial Trucks | Ted Britt Truck
    # Center" both touch tedbritttrucks.com, but only the second is the name.
    core = re.sub(r'[^a-z0-9]', '', domain.split('.')[0].lower())
    best, best_score = None, 0
    for c in cands:
        n = _clean_dealer_name(c)
        low = n.lower()
        if not (3 <= len(n) <= 60) or low in _GENERIC_NAMES or '.com' in low or 'www.' in low:
            continue
        if not _name_matches_domain(n, domain):
            continue
        score = sum(len(w) for w in re.findall(r'[a-z0-9]+', low) if len(w) >= 3 and w in core)
        if score > best_score:          # strict: earlier (more authoritative) wins ties
            best, best_score = n, score
    return best

def harvest_vin_from_html(text, soup=None):
    """
    Pull the vehicle's VIN out of a VDP's HTML. Used for dealer platforms whose
    URLs carry a page ID instead of a VIN, so the report can still show one.
    Returns a validated VIN or None — never a guess.
    """
    try:
        for pat in _VIN_PATTERNS:
            for m in re.finditer(pat, text, re.IGNORECASE):
                cand = m.group(1).upper()
                if is_plausible_vin(cand):
                    return cand
        # Fallback: the most-repeated plausible VIN on the page, which is the
        # page's own vehicle. A single lone hit is accepted; a tie is not.
        counts = {}
        for m in re.finditer(r'\b([A-HJ-NPR-Z0-9]{17})\b', text.upper()):
            c = m.group(1)
            if is_plausible_vin(c):
                counts[c] = counts.get(c, 0) + 1
        if not counts:
            return None
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            return ranked[0][0]
    except Exception:
        pass
    return None

def check_universal_status(url, session):
    url = str(url).strip() # STRIP INVISIBLE SPACES FROM CSV
    year = get_year(url)
    vin = extract_vin(url)
    if not year: return "N/A"

    # WAF EVASION HEADERS
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0'
    ]

    try:
        headers = {
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1'
        }

        response = session.get(url, headers=headers, timeout=5, allow_redirects=True)

        if response.status_code in [403, 406, 429]:
            return f"ERROR (Website Firewall Blocked Scan: {response.status_code})"
            
        if response.status_code in [404, 410]:
            return "SOLD (404 Error)"
            
        orig_base = url.lower().split('?')[0].rstrip('/').replace('https://', '').replace('http://', '').replace('www.', '')
        final_base = response.url.lower().split('?')[0].rstrip('/').replace('https://', '').replace('http://', '').replace('www.', '')
        
        if orig_base != final_base:
            if vin != "N/A" and vin.lower() not in final_base: return "SOLD (HTTP Redirect)"
            if year not in final_base: return "SOLD (HTTP Redirect)"

        text = response.text 
        text_lower = text.lower()
        soup = BeautifulSoup(text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""

        # The dealership's own name, read once per domain from a page we already
        # fetched — so reports show the name the dealer uses, not a URL slug.
        try:
            _dom = _dealer_domain(url)
            if _dom and _dom not in HARVESTED_DEALER_NAMES:
                _nm = harvest_dealer_name_from_html(text, soup, _dom)
                if _nm:
                    HARVESTED_DEALER_NAMES[_dom] = _nm
        except Exception:
            pass

        # This dealer's links don't carry a VIN: take it off the page itself so
        # the report can still show one. Done before the status checks so a
        # vehicle sold behind an "out of stock" overlay gets its VIN too.
        if vin == "N/A":
            found_vin = harvest_vin_from_html(text, soup)
            if found_vin:
                HARVESTED_VINS[url_key(url)] = found_vin
        
        # --- SOFT-SOLD / OVERLAY / JSON SCANNER ---
        soft_sold_phrases = [
            "no longer available",
            "this vehicle is sold",
            "vehicle has been sold",
            "currently out of stock",
            "schema.org/outofstock",     
            "schema.org/soldout",        
            '"inventorystatus":"sold"',  
            '"inventorystatus": "sold"', 
            '"vehiclestatus":"sold"',    
            '"vehiclestatus": "sold"',
            '"isavailable":false',
            '"isavailable": false'
        ]
        if any(phrase in text_lower for phrase in soft_sold_phrases):
            return "SOLD (Out of Stock Overlay)"
        # ----------------------------------------
        
        bot_titles = ['just a moment', 'attention required', 'verify you are human', 'access denied', 'pardon our interruption', 'security check']
        if any(b in page_title for b in bot_titles): return "ERROR (Cloudflare Bot Block)"
            
        meta_refresh = soup.find('meta', attrs={'http-equiv': re.compile(r'^refresh$', re.I)})
        if meta_refresh and meta_refresh.get('content'):
            content = meta_refresh['content']
            match = re.search(r'url=([^"\'>\s]+)', content, re.IGNORECASE)
            if match:
                meta_url = match.group(1).lower()
                if vin != "N/A" and vin.lower() not in meta_url:
                    return "SOLD (Meta Refresh Redirect)"
                    
        js_redirects = re.findall(r'window\.location\.(?:replace|href|assign)\s*=\s*["\']([^"\'>]+)["\']', text, re.IGNORECASE)
        for js_url in js_redirects:
            if vin != "N/A" and vin.lower() not in js_url.lower():
                return "SOLD (JS Redirect)"

        if 'not found' in page_title or '404' in page_title or 'error' in page_title: return "SOLD (Page Not Found)"
            
        search_indicators = ['search', 'results', 'all vehicles', 'inventory']
        if any(x in page_title for x in search_indicators) and year not in page_title: return "SOLD (Soft Redirect)"
            
        return "Available"
        
    except requests.exceptions.Timeout: return "ERROR (Timeout)"
    except requests.exceptions.ConnectionError: return "ERROR (Connection Blocked)"
    except Exception as e: return "Available"


# --- UI DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent")
st.caption("See which vehicles your campaign audience viewed – and how fast they're selling.")

# Sidebar: File Upload
st.sidebar.markdown("### 📥 New Analysis")
uploaded_file = st.sidebar.file_uploader("Upload Traffic Report (CSV)", type=['csv'], key="csv_upload")

# --- BUILD JOB: survives page restarts ---
# Streamlit restarts the script whenever any control changes, which used to
# cancel a scan in progress (and lose its work). A build is now a job held in
# session state: a restart resumes it, skipping vehicles already checked.
def _file_sig(f):
    return (getattr(f, 'name', None), getattr(f, 'size', None)) if f is not None else None

def _start_build():
    st.session_state['_scan_job'] = {'file': _file_sig(st.session_state.get('csv_upload')),
                                     'attempts': 0}
    st.session_state['_scan_partial'] = {}

_scan_job = st.session_state.get('_scan_job')
if _scan_job and _scan_job.get('file') != _file_sig(uploaded_file):
    st.session_state.pop('_scan_job', None)          # file removed or swapped: drop the job
    st.session_state.pop('_scan_partial', None)
    _scan_job = None
_scan_busy = bool(_scan_job)

# --- REPORT DETAILS (lookback window + visitor engagement) ---
# Sits between the upload and the Run button so it's part of the normal flow,
# not a hidden drawer. Still optional; can also be filled in on the report.
with st.sidebar.container(border=True):
    st.markdown("**🗓️ Report Details**")
    st.caption("🔒 Locked while your report builds - you can edit these on the report "
               "once it's ready." if _scan_busy else
               "Recommended. Adds the lookback window and pages per visitor to the "
               "dashboard and downloads.")
    st.selectbox(
        "Campaign month", disabled=_scan_busy, placeholder="— Not specified —",
        options=report_month_options(),
        format_func=format_report_month,
        key="report_month_sel",
        help="The month the uploaded traffic data covers. Used to calculate how long "
             "the inventory had to move before this scan."
    )
    st.number_input(
        "Total unique visitors", disabled=_scan_busy,
        min_value=0, step=1, key="unique_visitors_input",
        help="From the Unified Dashboard. Enables the average pages-per-visitor stat."
    )

run_analysis_clicked = False
if uploaded_file is not None:
    st.sidebar.button("🚀 Build Inventory Impact Report", type="primary", use_container_width=True,
                      on_click=_start_build, disabled=_scan_busy)
    run_analysis_clicked = _scan_busy
    st.sidebar.info("Upload a CSV and click Run. If we need help locating the dealer's inventory database, we'll give you simple steps to sync it dynamically!")

# --- ADD A DEALER TO THE VAULT (salesperson-friendly) ---
with st.sidebar.expander("🔐 Add Dealer to Vault", expanded=False):
    st.caption("Only needed if a dealer's inventory won't scan on its own. Pick the easiest option that works.")
    tab_auto, tab_book, tab_manual = st.tabs(["⚡ Auto-Detect", "🔖 Bookmarklet", "✍️ Manual"])

    # ---- Option A: Auto-Detect (easiest; may be blocked by strict firewalls) ----
    with tab_auto:
        st.markdown("<span style='font-size:0.85em;color:#555;'>Type the dealer's website and we'll try to find the keys automatically.</span>", unsafe_allow_html=True)
        ad_domain = st.text_input("Dealer website", placeholder="e.g., pittstoyota.com", key="ad_domain")
        if st.button("⚡ Auto-Detect & Save", use_container_width=True, key="ad_btn"):
            dom = normalize_domain(ad_domain)
            if not dom:
                st.warning("Enter the dealer's website first.")
            else:
                with st.spinner(f"Searching {dom} for inventory keys..."):
                    creds = autodetect_algolia(dom)
                if creds:
                    if upsert_vault_row(dom, creds['app_id'], creds['api_key'], creds['index']):
                        st.success(f"✅ {dom} synced automatically!")
                        st.rerun()
                    else:
                        st.error("Found the keys, but couldn't write to the Google Sheet.")
                else:
                    st.warning("Couldn't auto-detect (this dealer's firewall likely blocked us). "
                               "Use the **🔖 Bookmarklet** tab — it runs in your own browser and works even when this doesn't.")

    # ---- Option B: Bookmarklet (most reliable; runs in the salesperson's browser) ----
    with tab_book:
        st.markdown(
            "<span style='font-size:0.85em;color:#555;'>"
            "<b>One-time setup:</b> drag the button below up to your browser's bookmarks bar."
            "</span>", unsafe_allow_html=True)

        safe_href = html.escape(BOOKMARKLET_JS, quote=True)
        components.html(
            f"""
            <div style="font-family:sans-serif;padding:4px 0;">
              <a href="{safe_href}"
                 style="display:inline-block;background:#D70015;color:#fff;text-decoration:none;
                        font-weight:bold;padding:8px 14px;border-radius:6px;border:1px solid #A30010;
                        cursor:grab;font-size:14px;">
                 🔑 Get Dealer Keys
              </a>
              <div style="font-size:12px;color:#777;margin-top:6px;">☝️ Drag me to your bookmarks bar</div>
            </div>
            """,
            height=70,
        )

        st.markdown(
            "<span style='font-size:0.85em;color:#555;'>"
            "<b>Every time:</b> open the dealer's website → click your <b>Get Dealer Keys</b> bookmark. "
            "If it says the Search Key is missing, type any letter in the dealer's inventory search box "
            "so a search runs, then click the bookmark <b>again</b>. It copies a line → paste it below and Save."
            "</span>", unsafe_allow_html=True)

        with st.popover("Can't drag it? Copy the code instead"):
            st.caption("Create a new bookmark, name it 'Get Dealer Keys', and paste this as the URL/address:")
            st.code(BOOKMARKLET_JS, language=None)

        book_paste = st.text_area("Paste the copied line here", placeholder="fivestarfordga.com|APPID|APIKEY|indexname",
                                  key="book_paste", height=80)
        if st.button("💾 Save to Vault", use_container_width=True, key="book_btn"):
            creds = parse_algolia_credentials(book_paste)
            if creds and creds['domain']:
                if upsert_vault_row(creds['domain'], creds['app_id'], creds['api_key'], creds['index']):
                    st.success(f"✅ {creds['domain']} synced to the vault!")
                    st.rerun()
                else:
                    st.error("Couldn't write to the Google Sheet.")
            else:
                st.error("That didn't look right. Click the bookmark on the dealer's site, then paste what it copies.")

    # ---- Option C: Manual (power users / DevTools method) ----
    with tab_manual:
        st.markdown("<span style='font-size:0.85em;color:#555;'>Paste a full Algolia API URL (or any text containing the keys).</span>", unsafe_allow_html=True)
        qd_domain = st.text_input("Dealer website", placeholder="e.g., pittstoyota.com", key="qd_domain")
        qd_url = st.text_area("Algolia API URL / config", placeholder="Paste here...", key="qd_url", height=100)
        if st.button("💾 Save to Vault", use_container_width=True, key="manual_btn"):
            if qd_domain and qd_url:
                creds = parse_algolia_credentials(qd_url, fallback_domain=qd_domain)
                if creds:
                    dom = creds['domain'] or normalize_domain(qd_domain)
                    if upsert_vault_row(dom, creds['app_id'], creds['api_key'], creds['index']):
                        st.success(f"✅ {dom} synced to the vault!")
                        st.rerun()
                    else:
                        st.error("Couldn't write to the Google Sheet. Check the connection and try again.")
                else:
                    st.error("Couldn't find all three keys (App ID, API Key, Index) in what you pasted.")
            else:
                st.warning("Please fill both fields.")

    # ---- Diagnostic: is a dealer's key actually working? ----
    st.divider()
    st.markdown("<span style='font-size:0.85em;color:#555;'><b>🔬 Test a dealer's keys</b> — check if the API actually works (or why it doesn't).</span>", unsafe_allow_html=True)
    test_domain = st.text_input("Domain to test", placeholder="e.g., fivestarfordga.com", key="test_domain")
    test_paste = st.text_area("(optional) paste a bookmarklet line to test before saving", key="test_paste", height=68,
                              placeholder="Leave blank to test what's already saved in the vault")
    if st.button("🔬 Run Key Test", use_container_width=True, key="test_btn"):
        dom = normalize_domain(test_domain)
        creds = parse_algolia_credentials(test_paste, fallback_domain=dom) if test_paste.strip() else None
        source = "pasted line"
        if not creds:
            cfg = DEALER_API_VAULT.get(dom)
            source = "the vault"
            if not dom:
                st.warning("Enter a domain (or paste a line) to test.")
                cfg = None
            elif cfg is None:
                st.error(f"❌ '{dom}' is NOT in the vault — nothing was saved. Add it via Auto-Detect or the Bookmarklet.")
                cfg = None
            elif isinstance(cfg, dict) and cfg.get('ignore'):
                st.error(f"🚫 '{dom}' is set to IGNORE in the sheet, so the app skips the API entirely. "
                         f"Re-save real keys for it to overwrite the IGNORE flag.")
                cfg = None
            if cfg:
                creds = {'app_id': cfg.get('app_id'), 'api_key': cfg.get('api_key'), 'index': cfg.get('index')}
        if creds:
            with st.spinner("Testing keys against Algolia..."):
                ok, headline, detail = test_algolia_creds(creds['app_id'], creds['api_key'], creds['index'], domain=dom)
            (st.success if ok else st.error)(f"{headline}  \n_{detail}_  \n(tested from {source})")

# Main Execution Logic
if run_analysis_clicked:
    _scan_job['attempts'] = _scan_job.get('attempts', 0) + 1
    if _scan_job['attempts'] > 4:
        st.session_state.pop('_scan_job', None)
        st.session_state.pop('_scan_partial', None)
        st.error("This report kept getting interrupted before it could finish. Please click "
                 "**Build Inventory Impact Report** again and keep this tab open until it's done.")
        st.stop()
    df_raw = pd.read_csv(uploaded_file)
    uploaded_file.seek(0)
    
    url_col = 'Page Url' if 'Page Url' in df_raw.columns else 'Page URL' if 'Page URL' in df_raw.columns else df_raw.columns[0]
    df_raw.rename(columns={url_col: 'Page Url'}, inplace=True)

    # Visits column: the Unified Dashboard exports "Attributed Unique Visitors",
    # but merged/rebuilt files often use another header ("Unique Visits" etc.).
    # Detect it by name, then fall back to the first numeric column.
    if 'Attributed Unique Visitors' not in df_raw.columns:
        _visit_col = None
        _aliases = ['unique visits', 'unique visitors', 'attributed unique visits',
                    'attributed visitors', 'attributed visits', 'visits', 'visitors',
                    'total visits', 'sessions', 'unique page visits', 'page views']
        _lookup = {str(c).strip().lower(): c for c in df_raw.columns}
        for _a in _aliases:
            if _a in _lookup:
                _visit_col = _lookup[_a]
                break
        if _visit_col is None:
            for _c in df_raw.columns:
                if _c == 'Page Url':
                    continue
                _test = pd.to_numeric(
                    df_raw[_c].astype(str).str.replace(r'[,\s]', '', regex=True),
                    errors='coerce')
                if _test.notna().sum() >= max(1, int(len(df_raw) * 0.8)):
                    _visit_col = _c
                    break
        if _visit_col is None:
            st.session_state.pop('_scan_job', None)
            st.error("❌ Couldn't find a visits column in this CSV. Expected "
                     "**Attributed Unique Visitors** (or Unique Visits / Visitors). "
                     f"Columns found: {', '.join(str(c) for c in df_raw.columns)}")
            st.stop()
        df_raw.rename(columns={_visit_col: 'Attributed Unique Visitors'}, inplace=True)
        if str(_visit_col) != 'Attributed Unique Visitors':
            st.caption(f"Using **{_visit_col}** as the visits column.")

    # Numbers may carry thousands separators or come from projected/merged data.
    df_raw['Attributed Unique Visitors'] = pd.to_numeric(
        df_raw['Attributed Unique Visitors'].astype(str).str.replace(r'[,\s]', '', regex=True),
        errors='coerce').fillna(0).round().astype(int)
    
    df_raw = df_raw.dropna(subset=['Page Url'])
    df_raw = df_raw[df_raw['Page Url'].astype(str).str.strip() != '']
    
    df_raw['Category'] = df_raw['Page Url'].apply(categorize)
    
    unique_domains_list = df_raw['Page Url'].apply(lambda x: urlparse(str(x)).netloc.replace('www.', '').lower()).unique()
    unique_domains_list = [d for d in unique_domains_list if d]
    is_multi_dealer = len(unique_domains_list) > 1
    
    df_raw['Dealer'] = df_raw['Page Url'].apply(lambda x: dealer_display_name(x, is_multi_dealer)) 
    vdp_urls = df_raw[df_raw['Category'] == 'VDP']['Page Url'].tolist()
    
    _partial = st.session_state.setdefault('_scan_partial', {})
    vdp_results = _partial.setdefault('vdp_results', {})   # survives a page restart
    _done = sum(1 for u in vdp_urls if u in vdp_results)
    if _done:
        st.info(f"Picking up where the scan left off - {_done:,} of {len(vdp_urls):,} vehicles "
                f"already checked...")
    else:
        st.info(f"Scanning {len(vdp_urls):,} vehicles for current availability...")
    st.caption("Keep this tab open until your report appears - a large dealer group can take a few minutes.")
    progress_bar = st.progress(_done / len(vdp_urls) if vdp_urls else 0)
    
    session = requests.Session()
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=60, pool_maxsize=60)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    
    # Per-run blacklist of domains whose API blocked us (403/401/429). Once a
    # domain lands here, every remaining VDP for it skips the API entirely.
    ignored_domains = set()
    ignore_lock = threading.Lock()

    # Resolve vaulted dealers' inventory in bulk before the per-car scan.
    if 'vin_status' in _partial:
        vin_status = _partial['vin_status']
    else:
        with st.spinner("Checking dealer inventories..."):
            vin_status = build_all_vin_status(vdp_urls, session)
        _partial['vin_status'] = vin_status

    _todo = [u for u in dict.fromkeys(vdp_urls) if u not in vdp_results]
    total_urls = len(vdp_urls)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=30)
    future_to_url = {
        executor.submit(scan_url, url, session, ignored_domains, ignore_lock, vin_status): url
        for url in _todo
    }
    # Update the progress bar at most ~100 times, not once per URL — on a
    # 1,000+ car report, per-URL updates flood the UI and slow the run.
    progress_step = max(1, total_urls // 100)
    try:
        for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
            url = future_to_url[future]
            try:
                vdp_results[url] = future.result()
            except Exception:
                # A single malformed URL must never abort a large report.
                vdp_results[url] = "ERROR (Scan Failed)"
            n_done = _done + i + 1
            if n_done % progress_step == 0 or n_done == total_urls:
                progress_bar.progress(min(n_done / total_urls, 1.0))
    finally:
        # If the page restarts mid-scan, keep whatever already finished and drop
        # the queued work so the restart is immediate (it used to wait for every
        # remaining vehicle, then throw the results away).
        for _f, _u in future_to_url.items():
            if _u not in vdp_results and _f.done() and not _f.cancelled():
                try:
                    vdp_results[_u] = _f.result()
                except Exception:
                    pass
        executor.shutdown(wait=False, cancel_futures=True)

    # --- EXTERNAL FALLBACK: resolve anything the normal scan couldn't ------
    # Locked dealers (API rejected + firewall-blocked) leave "ERROR" results.
    # If a MarketCheck key is set, resolve those via MarketCheck's daily inventory.
    mc_calls_used = 0
    mc_key = get_marketcheck_key()
    mc_by_domain = defaultdict(list)   # domain -> [(url, vin)]
    for url, res in vdp_results.items():
        if str(res).startswith("ERROR") and "No VIN" not in str(res):
            v = extract_vin(url)
            # Vehicles without a readable VIN are still included: dealers whose
            # VDP links carry page IDs instead of VINs are resolved by matching
            # those links against the live listings (see _mc_apply_by_url).
            dom = urlparse(str(url)).netloc.replace('www.', '').lower()
            mc_by_domain[dom].append((url, v))

    # Blocked-site signature check: only dealers with MC_MIN_UNRESOLVED or more
    # unchecked vehicles qualify — a handful of stragglers on an otherwise
    # healthy site is scan noise, not a blocked website.
    skipped_stragglers = sum(len(its) for d, its in mc_by_domain.items()
                             if len(its) < MC_MIN_UNRESOLVED)
    mc_by_domain = defaultdict(list, {d: its for d, its in mc_by_domain.items()
                                      if len(its) >= MC_MIN_UNRESOLVED})
    if skipped_stragglers:
        st.caption(f"ℹ️ {skipped_stragglers} vehicle(s) on otherwise-healthy dealer sites "
                   f"couldn't be checked and were left as-is (below the "
                   f"{MC_MIN_UNRESOLVED}-vehicle threshold for market lookups).")
    if mc_by_domain:   # there are locked cars to try to rescue
        if not mc_key:
            st.info("A few dealers couldn't be scanned directly. To resolve them, "
                    "the market-inventory lookup needs to be set up in settings.")
        else:
            total_unres = sum(len(v) for v in mc_by_domain.values())
            st.info(f"{len(mc_by_domain)} dealer(s) with {total_unres} vehicle(s) need the "
                    f"market-inventory lookup — this run will use at most {MC_BUDGET} lookups.")
            MC_AUTO_PASSES = 4 if MC_AUTO_CONTINUE else 1
            month_base = st.session_state.get('mc_month_usage') or 0
            resolved = 0
            stop_reason = None
            cache_hits = 0
            mc_cache_out = {}
            mc_cache = {}
            if MC_CACHE_DAYS > 0 and gsheets_conn is not None:
                mc_cache = _mc_cache_load(gsheets_conn)
            total_calls = 0
            pending = {dom: list(its) for dom, its in mc_by_domain.items()}
            gov = None
            mc_pass = 0
            for mc_pass in range(MC_AUTO_PASSES):
                gov = _MCGovernor(session, MC_BUDGET,
                                  month_used=month_base + total_calls,
                                  month_cap=MC_MONTH_CAP)
                stop_reason = None
                pass_start_resolved = resolved
                try:
                    if mc_pass == 0:
                        # Cheap reachability probe (1 call): a bad key or exhausted
                        # plan halts here with a clear reason instead of failing
                        # silently. Skipped when the dealer-inventory memory covers
                        # every vehicle — a fully-cached repeat costs ZERO lookups.
                        needs_live = False
                        for _dom, _its in pending.items():
                            _c = mc_cache.get(_dom)
                            if _c is None or _c["age"] > MC_CACHE_DAYS:
                                needs_live = True
                                break
                            if not _c["complete"]:
                                for _u, _v in _its:
                                    _vv = str(_v).upper().strip()
                                    if _vv not in _c["active"] and _vv not in _c["absent"]:
                                        needs_live = True
                                        break
                            if needs_live:
                                break
                        if needs_live:
                            gov.get("https://api.marketcheck.com/v2/search/car/active",
                                    params={"api_key": mc_key, "source": next(iter(pending)), "rows": "1"},
                                    timeout=15)
                    spin_msg = ("Checking remaining vehicles against live market inventory..."
                                if mc_pass == 0 else
                                f"Large dealer: continuing lookups automatically (pass {mc_pass + 1})...")
                    with st.spinner(spin_msg):
                        # Smallest dealers first: they're the cheapest to finish, so a
                        # tight budget fully resolves as many dealers as possible
                        # before it runs out (whole dealers beat everyone-half-done).
                        for dom, items in sorted(pending.items(), key=lambda kv: len(kv[1])):
                            cached = mc_cache.get(dom)
                            if cached is not None and cached["age"] <= MC_CACHE_DAYS:
                                # Serve everything the memory knows at zero cost.
                                live_items = []
                                for url, v in items:
                                    vv = str(v).upper().strip()
                                    if vv in cached["active"]:
                                        vdp_results[url] = "Available"
                                    elif cached["complete"] or vv in cached["absent"]:
                                        vdp_results[url] = "SOLD (Not in Market Inventory)"
                                    else:
                                        live_items.append((url, v))
                                        continue
                                    resolved += 1
                                    cache_hits += 1
                                if not live_items:
                                    continue
                                items = live_items   # only unknowns go to live lookups
                            year_items = [(url, v, get_year(url)) for url, v in items]
                            dom_out = {}
                            try:
                                resolve_dealer_marketcheck(dom, year_items, mc_key, gov,
                                                           cache_out=mc_cache_out,
                                                           out=dom_out)
                            finally:
                                # A governor stop mid-dealer must never discard the
                                # answers already collected for this dealer.
                                for url, status in dom_out.items():
                                    vdp_results[url] = status
                                    resolved += 1
                except _MCStop as stop:
                    stop_reason = stop.reason
                total_calls += gov.count
                # Anything still unresolved rolls into the next pass.
                new_pending = {}
                for dom, its in pending.items():
                    left = [(url, v) for url, v in its
                            if str(vdp_results.get(url, "ERROR")).startswith("ERROR")]
                    if left:
                        new_pending[dom] = left
                pending = new_pending
                if not pending:
                    break                       # everything resolved
                if stop_reason != "budget":
                    break                       # real halt (limit/errors/monthly)
                if resolved == pass_start_resolved:
                    break                       # zero progress: never loop-burn
            mc_calls_used = total_calls
            leftover = total_unres - resolved
            if mc_cache_out and MC_CACHE_DAYS > 0 and gsheets_conn is not None:
                _mc_cache_save(gsheets_conn, mc_cache_out, max_age_days=MC_CACHE_DAYS)
            if cache_hits:
                st.caption(f"🗂️ {cache_hits} vehicle(s) resolved from the dealer-inventory "
                           f"memory (snapshots under {MC_CACHE_DAYS} days old) — 0 lookups spent.")
            if mc_pass > 0:
                st.caption(f"🔁 Large dealer detected: lookups continued automatically across "
                           f"{mc_pass + 1} budget passes ({mc_calls_used} total lookups).")
            err_detail = getattr(gov, "last_error", None)
            if stop_reason == "limit":
                st.warning(f"The market-inventory service declined further lookups (monthly "
                           f"plan limit reached, or the key needs attention). Resolved "
                           f"{resolved} vehicle(s) before stopping; {leftover} left unresolved."
                           + (f" Last response — {err_detail}" if err_detail else ""))
            elif stop_reason == "budget":
                st.warning(f"Reached this run's market-lookup budget of {MC_BUDGET}. Resolved "
                           f"{resolved} vehicle(s); {leftover} left unresolved. The budget can "
                           f"be raised in settings if needed.")
            elif stop_reason == "monthly":
                st.warning(f"Paused market lookups: the month-wide cap of {MC_MONTH_CAP} "
                           f"(MARKETCHECK_MONTHLY_CAP) is reached. Resolved {resolved} "
                           f"vehicle(s); {leftover} left unresolved this run.")
            elif stop_reason == "errors":
                st.warning(f"The market-inventory lookup stopped after repeated errors. "
                           f"Resolved {resolved} vehicle(s); {leftover} left unresolved."
                           + (f" Last error — {err_detail}" if err_detail else ""))
            elif resolved:
                st.success(f"✅ Resolved {resolved} additional vehicle(s) from live market "
                           f"inventory ({mc_calls_used} lookups used).")
                if err_detail:
                    st.caption(f"⚠️ Some lookups fell back to a slower method — last error: "
                               f"{err_detail}")
            elif err_detail:
                st.error(f"Market-inventory lookups are failing — {leftover} vehicle(s) left "
                         f"unresolved. Last error — {err_detail}")
            else:
                st.info("A few dealers couldn't be matched in the market inventory.")


    # Names read off the dealers' own pages are only known after the scan.
    try:
        harvest_names_for_unnamed(df_raw['Page Url'].tolist(), session)
    except Exception:
        pass
    try:
        _name_map = resolve_dealer_names(df_raw['Page Url'].tolist(), is_multi_dealer)
        df_raw['Dealer'] = df_raw['Page Url'].apply(
            lambda u: _name_map.get(_dealer_domain(u)) or dealer_display_name(u, is_multi_dealer))
    except Exception:
        pass
    df_raw['Sold_Status'] = df_raw['Page Url'].map(vdp_results).fillna('N/A')
    df = df_raw.copy()
    
    df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
    df['Vehicle Name'] = df['Page Url'].apply(clean_name_universal)
    df['VIN'] = df['Page Url'].apply(
        lambda u: HARVESTED_VINS.get(url_key(u)) or extract_vin(u))
    df['Type'] = df['Page Url'].apply(extract_type)

    # --- SANITY GUARD (data quality only): if nearly everything reads SOLD AND
    # we couldn't read real VINs off this dealer's pages, the matching itself
    # failed (e.g. a site whose VDP URLs carry page IDs rather than VINs) and
    # the "sales" are false. Withhold those.
    # NOTE: a high sold rate on its own is NOT an error — an older report
    # naturally shows most cars sold. That case is handled by a dismissible
    # advisory in the results view, and the data is left intact.
    try:
        _checked = df[df['Sold_Status'].astype(str).str.match(r'SOLD|Available')]
        _dom_of = lambda u: urlparse(str(u)).netloc.replace('www.', '').lower()
        for _dom, _grp in _checked.groupby(df['Page Url'].apply(_dom_of)):
            if len(_grp) < 10:
                continue
            _sold_rate = _grp['Sold_Status'].astype(str).str.startswith('SOLD').mean()
            _bad_vins = (_grp['Page Url'].apply(extract_vin) == 'N/A').mean()
            if _sold_rate >= 0.9 and _bad_vins > 0.5:
                _idx = df['Page Url'].apply(_dom_of) == _dom
                df.loc[_idx & df['Is Sold'], 'Sold_Status'] = 'ERROR (Verification Failed)'
                df.loc[_idx, 'Is Sold'] = False
                st.error(f"⚠️ **Results withheld for {_dom}.** {int(_sold_rate * 100)}% of "
                         f"viewed vehicles came back as sold, and this dealer's vehicle "
                         f"pages don't publish VINs we can match — so those \"sales\" can't "
                         f"be verified. They're marked unverified rather than sold, so "
                         f"nothing incorrect reaches a client. Please spot-check a few VDP "
                         f"links and report this dealer so we can add support for it.")
    except Exception:
        pass
    
    vdp_mask = df['Category'] == 'VDP'
    df.loc[vdp_mask, 'Category'] = df.loc[vdp_mask, 'Type'] + ' VDP'
    
    df['Est. Value'] = df.apply(estimate_value, axis=1)
    df['Price Tier'] = df['Est. Value'].apply(get_price_tier)
    
    domain_count = df['Dealer'].nunique()
    eastern = pytz.timezone('US/Eastern')
    report_time = datetime.datetime.now(eastern).strftime('%I:%M %p ET')

    if domain_count > 1:
        report_id = f"Auto Group ({domain_count} Sites) - {report_time}"
    else:
        domain = df['Dealer'].iloc[0] if len(df) > 0 else "Unknown Dealer"
        report_id = f"{domain} ({report_time})"
    
    # --- USAGE TRACKER ---
    try:
        if gsheets_conn is not None:
            dealer_domains = sorted(set(
                urlparse(str(u)).netloc.replace('www.', '').lower() for u in vdp_urls
            ))
            usage_df = gsheets_conn.read(worksheet="UsageStats", ttl=0)
            new_usage = pd.DataFrame([{
                "Timestamp": str(datetime.datetime.now(eastern)),
                "Report": report_id,
                "Dealer URLs": ", ".join(dealer_domains),
                "MarketCheck Calls": mc_calls_used,
            }])
            updated_usage = pd.concat([usage_df, new_usage], ignore_index=True)
            gsheets_conn.update(worksheet="UsageStats", data=updated_usage)
            st.session_state.global_usage_count = len(updated_usage)
            if isinstance(st.session_state.get('mc_month_usage'), int):
                st.session_state.mc_month_usage += mc_calls_used
            elif mc_calls_used:
                st.session_state.mc_month_usage = mc_calls_used
    except Exception:
        pass 
    
    st.session_state.history[report_id] = df
    st.session_state.current_report_id = report_id
    st.session_state.pop('_scan_job', None)
    st.session_state.pop('_scan_partial', None)

    st.rerun()

# --- SIDEBAR: SESSION HISTORY MANAGER ---
# Sidebar tools that must be available BEFORE any report is run.
if is_admin():
  with st.sidebar.expander("📊 Market Lookup Status", expanded=False):
      st.markdown(f"**Lookup key:** {'✅ detected' if get_marketcheck_key() else '❌ not set'}")
      st.markdown(f"**Per-report budget:** `{MC_BUDGET}` lookups")
      st.markdown(f"**Inventory page cap:** `{MC_PAGE_CAP}` rows")
      st.markdown(f"**Cars per lookup:** `{MC_PAGE_SIZE}`")
      if isinstance(st.session_state.get('mc_month_usage'), int):
          cap_note = f" of `{MC_MONTH_CAP}` cap" if MC_MONTH_CAP else ""
          st.markdown(f"**Lookups this month:** `{st.session_state.mc_month_usage}`{cap_note} (from report log)")
      st.caption("Adjustable in settings via MARKETCHECK_BUDGET, "
                 "MARKETCHECK_PAGE_CAP, and MARKETCHECK_PAGE_SIZE.")

if is_admin():
  with st.sidebar.expander("🧪 Market Lookup Diagnostic", expanded=False):
      st.caption("Fires 5 tiny probes (5 lookups) to show which filters this API "
                 "key honors, plus a 1-call inventory test. Use a real dealer "
                 "domain and a VIN currently on their site.")
      diag_domain = st.text_input("Dealer domain", value="hamby.com", key="mc_diag_dom")
      diag_vin = st.text_input("A live VIN from that dealer", value="", key="mc_diag_vin")
      if st.button("Run diagnostic", key="mc_diag_btn"):
          dkey = get_marketcheck_key()
          if not dkey:
              st.error("No MarketCheck key configured.")
          else:
              import requests as _rq
              probes = [("source= (rows=1)", {"source": diag_domain.strip()}),
                        ("source= (rows=50, report-style)", {"source": diag_domain.strip(),
                                                            "rows": "50"}),
                        ("vins= (+source)", {"vins": diag_vin.strip().upper(),
                                             "source": diag_domain.strip()}),
                        ("vin= (+source)", {"vin": diag_vin.strip().upper(),
                                            "source": diag_domain.strip()}),
                        # 1-call inventory test: zero listings, every VIN as a facet
                        ("facets=vin (1-call inventory)", {"source": diag_domain.strip(),
                                                           "rows": "0",
                                                           "facets": "vin|0|1000"})]
              # Second probe replicates the report's store call exactly:
              # same params AND the same session construction.
              _diag_sess = _rq.Session()
              try:
                  _retry = Retry(total=3, backoff_factor=1,
                                 status_forcelist=[429, 500, 502, 503, 504])
                  _ad = HTTPAdapter(max_retries=_retry)
                  _diag_sess.mount('https://', _ad)
              except Exception:
                  pass
              for label, extra in probes:
                  if "vin" in label and not diag_vin.strip():
                      st.markdown(f"**{label}** — skipped (no VIN entered)")
                      continue
                  try:
                      _getter = _diag_sess.get if "report-style" in label else _rq.get
                      # Facets exist only on the search endpoint; test it there no
                      # matter which endpoint the report is configured to use.
                      _target = _MC_DEFAULT_ENDPOINT if "facets" in label else MC_ENDPOINT
                      pr = _getter(_target, params={"api_key": dkey, "rows": "1",
                                                    "start": "0", **extra}, timeout=20)
                      if pr.status_code == 200:
                          body = pr.json()
                          nf = body.get("num_found", "?")
                          if "facets" in label:
                              fac = (body.get("facets") or {}).get("vin") or []
                              st.markdown(f"**{label}** — HTTP 200, num_found = `{nf}`, "
                                          f"VINs returned as facets = `{len(fac)}`")
                              if fac and nf not in ("?", None) and len(fac) >= min(int(nf), 1000) * 0.9:
                                  st.caption("✅ Facets returned (nearly) the whole store in one "
                                             "call — a 1-call inventory pull is viable for this "
                                             "dealer.")
                              elif fac:
                                  st.caption("Facets returned a partial list — MarketCheck caps "
                                             "the facet length below this store's size.")
                              else:
                                  st.caption("No VIN facets returned — this key/endpoint doesn't "
                                             "support facets=vin.")
                          else:
                              st.markdown(f"**{label}** — HTTP 200, num_found = `{nf}`")
                      else:
                          st.markdown(f"**{label}** — HTTP {pr.status_code}: "
                                      f"`{str(pr.text)[:120]}`")
                  except Exception as e:
                      st.markdown(f"**{label}** — {type(e).__name__}: {str(e)[:120]}")
                  time.sleep(0.25)
              st.caption("How to read this: a filter is honored when num_found is "
                         "small (dealer-sized for source=, ~1-5 for vin=). A number "
                         "in the millions means that filter is being ignored.")


if st.session_state.history:
    st.sidebar.divider()
    st.sidebar.markdown("### 📂 Session History")
    
    report_names = list(st.session_state.history.keys())
    selected_report = st.sidebar.radio("Select a report to view:", options=report_names, index=report_names.index(st.session_state.current_report_id))
    if selected_report != st.session_state.current_report_id:
        st.session_state.current_report_id = selected_report
        st.rerun()
        
    st.sidebar.divider()
    if st.sidebar.button("🗑️ Clear History"):
        st.session_state.history = {}
        st.session_state.current_report_id = None
        st.rerun()
        
    st.sidebar.divider()
    st.sidebar.markdown(f"📈 **Total Global Scans:** `{st.session_state.global_usage_count}`")
# --- MAIN DASHBOARD DISPLAY ---
def dismissible_notice(notice_key, body, icon="🗓️"):
    """
    On-screen advisory the rep can close. Never written into the PDF/PPTX —
    this is guidance for reading the report, not a finding about the dealer.
    """
    flag = f"_dismissed_{notice_key}"
    if st.session_state.get(flag):
        return
    box = st.container()
    with box:
        col_msg, col_x = st.columns([30, 1])
        with col_msg:
            st.warning(f"{icon} {body}")
        with col_x:
            if st.button("✕", key=f"_dismiss_btn_{notice_key}", help="Dismiss this note"):
                st.session_state[flag] = True
                st.rerun()

# --- SIDEBAR: ADMIN UNLOCK (kept small; reps never need this) ---
if not is_admin():
    with st.sidebar.expander("🔧 Admin", expanded=False):
        if get_admin_key():
            _adm = st.text_input("Admin key", type="password", key="_admin_key_input")
            if _adm and _adm == get_admin_key():
                st.session_state["_is_admin"] = True
                st.rerun()
            elif _adm:
                st.caption("That key didn't match.")
        else:
            st.caption("Set ADMIN_KEY in settings to enable the admin tools.")

if st.session_state.current_report_id is not None:
    # Same framing as the downloads; the run time lives in Session History.
    st.subheader(f"Inventory Impact Report  |  {clean_report_title(st.session_state.current_report_id)}")
    _hdr_ctx = build_report_context(report_month=st.session_state.get('report_month_sel'))
    if _hdr_ctx.get('header_meta'):
        st.caption(_hdr_ctx['header_meta'])
    
    df = st.session_state.history[st.session_state.current_report_id].copy()

    # --- LOOKBACK ADVISORY (screen only, dismissible) ---
    # A long gap between the campaign month and the scan naturally pushes most
    # vehicles into "sold" — the report is still accurate, it just proves less.
    try:
        _rid = str(st.session_state.current_report_id)
        _months_back = None
        _rm = st.session_state.get('report_month_sel')
        if _rm:
            _now = datetime.datetime.now(eastern).date()
            _months_back = ((_now.year - int(_rm[0])) * 12 + (_now.month - int(_rm[1])))
        _vdp_rows = df[df['Sold_Status'].astype(str).str.match(r'SOLD|Available')]
        _sold_share = (_vdp_rows['Sold_Status'].astype(str).str.startswith('SOLD').mean()
                       if len(_vdp_rows) >= 10 else None)
        if _months_back is not None and _months_back > 3:
            dismissible_notice(
                f"lookback_{_rid}",
                f"**Long lookback window ({_months_back} months).** Over a window this "
                f"long most inventory turns over anyway, so a high sold count says less "
                f"about campaign impact. Use the **High-Engagement Filter** to call out "
                f"the vehicles our audience came back to most.")
        elif _sold_share is not None and _sold_share >= 0.9:
            dismissible_notice(
                f"soldrate_{_rid}",
                f"**{int(_sold_share * 100)}% of viewed vehicles came back sold.** If this "
                f"report covers a campaign month that closed a while ago, that's expected — "
                f"inventory turns over. Double-check the report period, and use the "
                f"**High-Engagement Filter** to call out the vehicles our audience came back to most.")
    except Exception:
        pass
    
    # 1. IN-APP ACTION CENTER FOR INVENTORY SYNC
    error_df = df[df['Sold_Status'].str.startswith('ERROR', na=False)].copy()
    if not error_df.empty:
        error_df['_base_domain'] = error_df['Page Url'].apply(lambda x: urlparse(str(x)).netloc.replace('www.', '').lower())
        troubleshoot_df = error_df.groupby(['Dealer', '_base_domain']).size().reset_index(name='Blocked Pages')
        severe_blocks = troubleshoot_df[troubleshoot_df['Blocked Pages'] >= 10]
        
        pending_blocks = severe_blocks[~severe_blocks['_base_domain'].isin(DEALER_API_VAULT.keys())]
        
        if len(severe_blocks) > 0:
            if len(pending_blocks) > 0:
                severe_count = len(pending_blocks)
                alert_text = f"⚙️ Action Required: Sync Inventory Database{'s' if severe_count > 1 else ''} ({severe_count} Remaining)"
                
                with st.expander(alert_text, expanded=True):
                    st.markdown("A few dealers hid their inventory behind a firewall. Sync them below to reveal true sales — easiest method first.")

                    st.markdown("---")
                    for _, row in pending_blocks.iterrows():
                        d_name = row['Dealer']
                        d_domain = row['_base_domain']
                        d_blocked = row['Blocked Pages']

                        st.markdown(f"👉 **[{d_name}](https://www.{d_domain})** *({d_blocked} unseen pages)*")
                        ca, cb = st.columns([5, 1])
                        with ca:
                            st.text_input(f"Keys for {d_domain}", key=f"inp_{d_domain}",
                                          label_visibility="collapsed",
                                          placeholder="Paste the line from the 🔑 bookmark (or an Algolia URL)...")
                        with cb:
                            if st.button("💾 Save", key=f"btn_{d_domain}", use_container_width=True):
                                val = st.session_state[f"inp_{d_domain}"]
                                if val:
                                    creds = parse_algolia_credentials(val, fallback_domain=d_domain)
                                    if creds:
                                        dom = creds['domain'] or d_domain
                                        if upsert_vault_row(dom, creds['app_id'], creds['api_key'], creds['index']):
                                            st.rerun()
                                        else:
                                            st.error("Couldn't write to the Google Sheet.")
                                    else:
                                        st.error("Couldn't find all 3 keys in that. Use the 🔑 bookmark on the dealer's site.")
                                else:
                                    st.warning("Paste the keys first!")

                        cc, cd = st.columns(2)
                        with cc:
                            if st.button("⚡ Auto-Detect", key=f"auto_{d_domain}", use_container_width=True,
                                         help="Let the app try to find the keys itself (a strict firewall may block this)."):
                                with st.spinner(f"Searching {d_domain}..."):
                                    creds = autodetect_algolia(d_domain)
                                if creds:
                                    if upsert_vault_row(d_domain, creds['app_id'], creds['api_key'], creds['index']):
                                        st.rerun()
                                    else:
                                        st.error("Found keys but couldn't write to the sheet.")
                                else:
                                    st.warning("Auto-detect blocked. Use the 🔑 bookmark below instead.")
                        with cd:
                            if st.button("🚫 Ignore", key=f"ign_{d_domain}",
                                         help="Mark as not scannable — the dealer doesn't use Dealer Inspire / Algolia, or can't be reached. Stops prompting for it.",
                                         use_container_width=True):
                                if upsert_vault_row(d_domain, 'IGNORE', 'IGNORE', 'IGNORE'):
                                    st.rerun()
                                else:
                                    st.error("Couldn't write the IGNORE flag to the Google Sheet.")
                        st.markdown("")

                    st.markdown("---")
                    st.markdown("**🔑 Easiest way — the one-click key grabber:**")
                    safe_href_ac = html.escape(BOOKMARKLET_JS, quote=True)
                    components.html(
                        f"""
                        <div style="font-family:sans-serif;padding:2px 0;">
                          <a href="{safe_href_ac}"
                             style="display:inline-block;background:#D70015;color:#fff;text-decoration:none;
                                    font-weight:bold;padding:8px 14px;border-radius:6px;border:1px solid #A30010;
                                    cursor:grab;font-size:14px;">🔑 Get Dealer Keys</a>
                          <span style="font-size:12px;color:#777;margin-left:8px;">☝️ one-time: drag me to your bookmarks bar</span>
                        </div>
                        """,
                        height=56,
                    )
                    st.markdown(
                        "1. **Once:** drag the button above to your bookmarks bar.\n"
                        "2. Click a dealer link above to open their site.\n"
                        "3. Click your **Get Dealer Keys** bookmark. If it says the Search Key is missing, "
                        "type any letter in the dealer's inventory search box, then click the bookmark **again**.\n"
                        "4. Paste the copied line into that dealer's box above and hit **Save**."
                    )

                    with st.popover("Advanced: find it manually (DevTools)"):
                        st.markdown("1. Open the dealer's site.\n2. Right-click → **Inspect**.\n3. **Network** tab → **Fetch/XHR** filter → refresh.\n4. Search `inventory`, click the **`query?`** result (contains 'algolia'), right-click → **Copy URL**, and paste it above.")
                        pdf_path = "Dealer Inspire URL Steps.pdf"
                        if os.path.exists(pdf_path):
                            with open(pdf_path, "rb") as pdf_file:
                                pdf_bytes = pdf_file.read()
                            st.download_button(
                                label="📄 Download Visual Guide (PDF)",
                                data=pdf_bytes,
                                file_name="Dealer_Inspire_URL_Steps.pdf",
                                mime="application/pdf"
                            )
            else:
                st.success("✅ All inventory syncs resolved! Click **Build Inventory Impact Report** in the sidebar to complete the rescan.")

    # 2. HIGH-ENGAGEMENT FILTER — one threshold, two ways to apply it.
    #    Highlight (default): the whole report stays on every viewed vehicle and
    #    the T+ visit vehicles are called out as a complementary subset.
    #    Filter: the original behaviour — every figure limited to T+ vehicles.
    #    Moving either control reruns from the stored scan: no re-scanning.
    _vdp_every = df[df['Category'].str.contains('VDP', na=False)]
    _MODE_LABELS = {'highlight': "✨ Highlight", 'filter': "🔎 Filter report", 'off': "Off"}
    _MODE_HELP = {
        'highlight': "The full report stays the headline; these sales are called out alongside "
                     "it - on screen and in the exports.",
        'filter': "Every number, chart and export is limited to these vehicles - for your most "
                  "discerning dealers.",
        'off': "High-engagement stats are left out of the dashboard and the exports. The report "
               "covers every viewed vehicle.",
    }
    _mode_now = st.session_state.get('vdp_mode') or 'highlight'
    st.markdown("<br>", unsafe_allow_html=True)
    with st.container(border=True):
        fc1, fc2 = st.columns([1.25, 1], gap="large")
        with fc1:
            st.markdown("##### 🎯 High-Engagement Filter")
            st.slider("Minimum campaign visits per vehicle", min_value=1, max_value=30, step=1,
                      key="he_threshold", disabled=(_mode_now == 'off'),
                      help="Vehicles that drew at least this many unique visits from the campaign "
                           "audience. These are the vehicles our audience came back to most - the "
                           "sales where our influence is clearest.")
        with fc2:
            _seg_kwargs = dict(options=list(_MODE_LABELS), format_func=lambda k: _MODE_LABELS[k],
                               key="vdp_mode")
            try:
                st.segmented_control("Show it in the report", required=True, **_seg_kwargs)
            except TypeError:                       # older Streamlit: no 'required'
                st.segmented_control("Show it in the report", **_seg_kwargs)
            st.caption(_MODE_HELP.get(st.session_state.get('vdp_mode') or 'highlight', ''))

        he_threshold = int(st.session_state.he_threshold)
        vdp_mode = st.session_state.get('vdp_mode') or 'highlight'
        if vdp_mode != 'off':
            # Live readout: how the subset reacts as the slider moves.
            _v = pd.to_numeric(_vdp_every['Attributed Unique Visitors'], errors='coerce').fillna(0)
            _s = _vdp_every['Is Sold'].astype(bool)
            _hv, _hs = int((_v >= he_threshold).sum()), int(((_v >= he_threshold) & _s).sum())
            _tv, _ts = len(_vdp_every), int(_s.sum())
            if he_threshold <= 1:
                _readout = ("At 1+ visits every viewed vehicle counts. Raise the threshold to "
                            "spotlight the vehicles our audience came back to most.")
            else:
                _readout = (f"At <b>{he_threshold}+ visits</b>: <b>{_hv:,}</b> of {_tv:,} viewed vehicles"
                            f" &nbsp;·&nbsp; <b>{_hs:,}</b> of {_ts:,} sold"
                            + (f" &nbsp;·&nbsp; <b>{_hs / _hv * 100:.1f}%</b> look-to-book" if _hv else ""))
            st.markdown(f"<div style='background:#EFF1FA;border-left:4px solid {PREMION_NAVY};"
                        f"padding:8px 12px;border-radius:4px;font-size:14px;color:#1f2937;'>"
                        f"{_readout}</div>", unsafe_allow_html=True)

    min_visitors = he_threshold if vdp_mode == 'filter' else 1
    st.markdown("---")

    # 3. APPLY FILTER (The "Velocity" Metric Update)
    vdp_df = df[(df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= min_visitors)]
    sold_df = vdp_df[vdp_df['Is Sold']]
    he = high_engagement_stats(vdp_df, he_threshold) if vdp_mode == 'highlight' else None
    if he is not None and he['viewed'] == 0:
        he = None

    new_vdp_all = vdp_df[vdp_df['Type'] == 'New']
    new_sold = sold_df[sold_df['Type'] == 'New']
    new_ltb = (len(new_sold) / len(new_vdp_all) * 100) if len(new_vdp_all) > 0 else 0

    used_vdp_all = vdp_df[vdp_df['Type'] == 'Used']
    used_sold = sold_df[sold_df['Type'] == 'Used']
    used_ltb = (len(used_sold) / len(used_vdp_all) * 100) if len(used_vdp_all) > 0 else 0

    m_units = len(sold_df)
    m_rev = sold_df['Est. Value'].sum()
    m_pipe = vdp_df['Est. Value'].sum()
    m_ltb = (len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0)

    if not sold_df.empty:
        avg_v = sold_df['Attributed Unique Visitors'].mean()
        missed_threshold = max(avg_v, min_visitors)
        missed_df = df[(df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= missed_threshold)].sort_values('Attributed Unique Visitors', ascending=False).head(10)
    else:
        missed_df = df[(df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= min_visitors)].sort_values('Attributed Unique Visitors', ascending=False).head(10)

    st.markdown("### 📊 Executive Summary")
    _pre_ctx = build_report_context(
        min_visitors=min_visitors,
        report_month=st.session_state.get('report_month_sel'),
        unique_visitors=st.session_state.get('unique_visitors_input', 0),
        total_visits=int(df['Attributed Unique Visitors'].sum())
    )
    st.caption("Vehicles our campaign audience viewed on the dealer's site.")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Viewed Vehicles Sold", m_units, help="Vehicles our campaign audience viewed that have since left the lot - influence and velocity, not purchases by our visitors.")
    m2.metric("Est. Value Sold", f"${m_rev:,.0f}")
    m3.metric("Est. Total Value Viewed", f"${m_pipe:,.0f}", help="Estimated value of every vehicle our audience viewed - sold, still listed, and unconfirmed. Sizes the in-market demand the campaign reached.")
    _chip = ltb_chip_text({'new_ltb': f"{new_ltb:.1f}", 'used_ltb': f"{used_ltb:.1f}",
                           'new_viewed': len(new_vdp_all), 'used_viewed': len(used_vdp_all)})
    m4.metric(label="Look-to-Book Ratio", value=f"{m_ltb:.1f}%",
              delta=_chip.replace("  |  ", " | ") if _chip else None, delta_color="off")

    # Optional reporting context (sidebar → "Report Details").
    report_ctx = _pre_ctx
    if report_ctx.get('threshold_line'):
        st.caption(f"🔎 {report_ctx['threshold_line']} Switch the High-Engagement Filter to "
                   f"*Highlight* to see the full report with these sales called out.")

    # Campaign context (Report Details). Sits right under the headline because
    # it qualifies it (the lookback explains how long inventory had to move);
    # the High-Engagement box below is a drill-down. When details are missing,
    # a quiet prompt with an inline editor - never a blocking message.
    def _sync_rm():
        st.session_state['report_month_sel'] = st.session_state.get('rm_inline')
    def _sync_uv():
        st.session_state['unique_visitors_input'] = int(st.session_state.get('uv_inline') or 0)
    st.session_state['rm_inline'] = st.session_state.get('report_month_sel')
    st.session_state['uv_inline'] = int(st.session_state.get('unique_visitors_input') or 0)

    _bits, _missing = [], []
    if report_ctx.get('unique_visitors_fmt'):
        _bits.append(f"<b>{report_ctx['unique_visitors_fmt']}</b> unique visitors")
        if report_ctx.get('avg_pages'):
            _bits.append(f"<b>{report_ctx['avg_pages']}</b> pages per visitor "
                         f"<span style='color:#6B7280;'>({report_ctx.get('total_visits_fmt', '')} page visits)</span>")
    else:
        _missing.append("unique visitors (for pages per visitor)")
    if report_ctx.get('lookback_short'):
        _bits.append(f"<b>{report_ctx['lookback_short']}</b> "
                     f"<span style='color:#6B7280;'>lookback</span>")
    elif not st.session_state.get('report_month_sel'):
        _missing.append("the campaign month (for the lookback window)")
    _line = " &nbsp;·&nbsp; ".join(_bits)
    if _missing:
        _ask = ("Add " + " and ".join(_missing) + " - it carries into the PDF and deck.")
        _line = (_line + " &nbsp;·&nbsp; " if _line else "") + f"<span style='color:#6B7280;'>{_ask}</span>"
    cx1, cx2 = st.columns([5, 1.1], vertical_alignment="center")
    with cx1:
        st.markdown(f"<div style='font-size:14px;color:#1f2937;padding:6px 0;'>🗓️ &nbsp;{_line}</div>",
                    unsafe_allow_html=True)
    with cx2:
        with st.popover("➕ Add details" if _missing else "✏️ Edit details", use_container_width=True):
            st.selectbox("Campaign month", options=report_month_options(),
                         format_func=format_report_month, key="rm_inline", on_change=_sync_rm)
            st.number_input("Total unique visitors", min_value=0, step=1,
                            key="uv_inline", on_change=_sync_uv,
                            help="From the Unified Dashboard.")

    # Secondary view: the high-engagement subset of the headline numbers.
    if he is not None:
        T = he['threshold']
        with st.container(border=True):
            st.markdown(f"**⭐ High-Engagement Sales** &nbsp;<span style='color:#6B7280;'>"
                        f"viewed vehicles with {T}+ campaign visits</span>", unsafe_allow_html=True)
            hl, hr = st.columns([1.15, 1], gap="large")
            with hl:
                k1, k2 = st.columns(2)
                k1.metric("High-Engagement Sold", f"{he['sold']:,}",
                          delta=(f"{he['sold_share_pct']:.0f}% of the {he['total_sold']:,} sold"
                                 if he['sold_share_pct'] is not None else None),
                          delta_color="off")
                k2.metric("Est. Value Sold", f"${he['value_sold']:,.0f}")
                k3, k4 = st.columns(2)
                k3.metric("Look-to-Book", f"{he['ltb']:.1f}%" if he['ltb'] is not None else "-",
                          delta=(f"{he['lift_pts']:+.1f} pts vs 1-{T - 1} visits"
                                 if he['lift_pts'] is not None else None),
                          delta_color="normal" if he['comparison_ok'] else "off")
                k4.metric("Vehicles Viewed", f"{he['viewed']:,}",
                          delta=f"of {he['total_viewed']:,} viewed", delta_color="off")
            with hr:
                _ltb_fig = he_ltb_plotly(he)
                if _ltb_fig is not None:
                    st.markdown("**Look-to-Book by Campaign Engagement**")
                    st.plotly_chart(_ltb_fig, use_container_width=True, config={'displayModeBar': False})
                else:
                    st.caption(f"Not enough vehicles on both sides of {T} visits to compare "
                               f"look-to-book fairly (needs {HE_MIN_GROUP_VIEWED}+ each).")
            # Tell the rep what the client will (and won't) see.
            if not he['export_ok']:
                st.caption(f"ℹ️ Fewer than {HE_MIN_SOLD_FOR_EXPORT} high-engagement sales - the "
                           f"exports leave this section out. Try a lower threshold.")
            elif not he['comparison_ok']:
                _why = he_comparison_reason(he)
                st.caption(f"ℹ️ Exports include these sales but not the look-to-book comparison: {_why}")
            else:
                st.caption("✅ Included in the PDF and PowerPoint: a summary strip on page 1 and a "
                           "High-Engagement Sales section.")

    st.divider()

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Traffic Mix**")
        traffic_data = df.groupby('Category')['Attributed Unique Visitors'].sum().reset_index().sort_values('Attributed Unique Visitors', ascending=False)
        traffic_data = traffic_data.rename(columns={'Attributed Unique Visitors': 'Unique Visits'})
        fig1 = px.bar(traffic_data, x='Category', y='Unique Visits', labels={'Unique Visits': 'Unique Visits'})
        st.plotly_chart(fig1, use_container_width=True)
    with c2:
        st.markdown("**Sales Mix (New vs Used)**")
        if not sold_df.empty:
            type_counts = sold_df['Type'].value_counts().reset_index()
            type_counts.columns = ['Type', 'Count']
            fig2 = px.pie(type_counts, values='Count', names='Type', color='Type', color_discrete_map=TYPE_COLORS, hover_data=['Count'])
            fig2.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig2, use_container_width=True)
            if he is not None and he['sold']:
                _cmp = mix_compare_plotly(sold_df['Type'].value_counts().to_dict(),
                                          he['sold_type_counts'], ['New', 'Used'], TYPE_COLORS,
                                          he['threshold'])
                if _cmp is not None:
                    st.caption(f"⭐ All sold vs. {he['threshold']}+ visit sales")
                    st.plotly_chart(_cmp, use_container_width=True, config={'displayModeBar': False})
    with c3:
        st.markdown("**Sold Value Tiers**")
        if not sold_df.empty:
            tier_counts = sold_df['Price Tier'].value_counts().reset_index()
            tier_counts.columns = ['Price Tier', 'Count']
            fig3 = px.pie(tier_counts, values='Count', names='Price Tier', color='Price Tier',
                          color_discrete_map=TIER_COLORS, category_orders={'Price Tier': TIER_ORDER},
                          hover_data=['Count'])
            fig3.update_traces(textposition='inside', textinfo='percent+label')
            st.plotly_chart(fig3, use_container_width=True)
            if he is not None and he['sold']:
                _cmp = mix_compare_plotly(sold_df['Price Tier'].value_counts().to_dict(),
                                          he['sold_tier_counts'], TIER_ORDER, TIER_COLORS,
                                          he['threshold'])
                if _cmp is not None:
                    st.caption(f"⭐ All sold vs. {he['threshold']}+ visit sales")
                    st.plotly_chart(_cmp, use_container_width=True, config={'displayModeBar': False})

    st.divider()

    domain_count = df['Dealer'].nunique()
    dealer_group_export = None

    if domain_count > 1:
        st.markdown(f"### 🏢 Auto Group Breakdown ({domain_count} Sites)")
        with st.container(border=True):
            g1, g2, g3 = st.columns([1, 1, 1.4], vertical_alignment="center")
            with g1:
                show_tier2 = st.toggle("📊 Store scoreboard", value=True,
                                       help="Group charts and the store-by-store table.")
            with g2:
                show_deep_dives = st.toggle("🔍 Store deep dives", value=False,
                                            help="A detail panel for every store, right below the scoreboard.")
            with g3:
                st.caption("Each store can also get its own page in the PDF and PowerPoint - "
                           "turn on **Dealer Deep Dives** under Export Reports.")

        t2_visitors = df.groupby('Dealer')['Attributed Unique Visitors'].sum().reset_index(name='Total Visitors')
        t2_vdps = vdp_df.groupby('Dealer').agg(
            VDPs_Shopped=('Category', 'count'),
            Pipeline_Value=('Est. Value', 'sum')
        ).reset_index()

        t2_sold = sold_df.groupby('Dealer').agg(
            Units_Sold=('Is Sold', 'count'),
            Est_Rev_Sold=('Est. Value', 'sum')
        ).reset_index()

        dealer_group = t2_visitors.merge(t2_vdps, on='Dealer', how='left').merge(t2_sold, on='Dealer', how='left').fillna(0)
        if he is not None:
            _he_sold = sold_df[sold_df['Attributed Unique Visitors'] >= he['threshold']]
            dealer_group = dealer_group.merge(
                _he_sold.groupby('Dealer').size().reset_index(name='High-Engagement Sold'),
                on='Dealer', how='left')
            dealer_group['High-Engagement Sold'] = dealer_group['High-Engagement Sold'].fillna(0).astype(int)
        dealer_group = dealer_group[dealer_group['VDPs_Shopped'] > 0].copy()
        # A traffic-only store (no vehicle pages) leaves blanks that fillna(0)
        # turns into decimals for the WHOLE column ("537.0" on the scoreboard),
        # even after that store is filtered out. Counts are whole numbers.
        dealer_group[['VDPs_Shopped', 'Units_Sold']] = (
            dealer_group[['VDPs_Shopped', 'Units_Sold']].astype(int))

        dealer_group['Look-to-Book (%)'] = dealer_group.apply(
            lambda row: round((row['Units_Sold'] / row['VDPs_Shopped'] * 100), 1) if row['VDPs_Shopped'] > 0 else 0.0,
            axis=1
        )

        dealer_group = dealer_group.rename(columns={
            'VDPs_Shopped': 'VDPs Shopped',
            'Units_Sold': 'Units Sold',
            'Est_Rev_Sold': 'Est. Rev Sold',
            'Pipeline_Value': 'Pipeline Value'
        })

        dealer_group_export = dealer_group.sort_values('Total Visitors', ascending=False)

        if show_tier2:
            cA, cB = st.columns(2)
            with cA:
                fig_traffic = px.bar(dealer_group_export.head(10), x='Dealer', y='Total Visitors', title='Top Dealers by Traffic')
                st.plotly_chart(fig_traffic, use_container_width=True)
            with cB:
                fig_sales = px.bar(dealer_group_export.sort_values('Units Sold', ascending=False).head(10), x='Dealer', y='Units Sold', title='Top Dealers by Viewed Vehicles Sold')
                st.plotly_chart(fig_sales, use_container_width=True)

            display_group = dealer_group_export.copy()
            display_group['Est. Rev Sold'] = display_group['Est. Rev Sold'].apply(lambda x: f"${x:,.0f}")
            display_group['Pipeline Value'] = display_group['Pipeline Value'].apply(lambda x: f"${x:,.0f}")
            display_group = display_group.rename(columns={
                'VDPs Shopped': 'VDPs Viewed',
                'Units Sold': 'Viewed Vehicles Sold',
                'Est. Rev Sold': 'Est. Value Sold',
                'Pipeline Value': 'Est. Total Value Viewed',
                'High-Engagement Sold': f"{he_threshold}+ Visit Sold"})

            st.dataframe(display_group, column_config={
                "Look-to-Book (%)": st.column_config.NumberColumn(format="%.1f%%")
            }, use_container_width=True, hide_index=True)

        if show_deep_dives:
            st.markdown("#### 🔍 Store Deep Dives")
            expand_all = st.toggle("Expand all stores", value=False)
            for dealer_name in dealer_group_export['Dealer']:
                _gr = dealer_group_export[dealer_group_export['Dealer'] == dealer_name].iloc[0]
                _he_bit = (f" · {int(_gr['High-Engagement Sold'])} with {he_threshold}+ visits"
                           if 'High-Engagement Sold' in dealer_group_export.columns else "")
                with st.expander(f"🏢 {dealer_name} — {int(_gr['Units Sold'])} sold · "
                                 f"{_gr['Look-to-Book (%)']}% look-to-book{_he_bit}", expanded=expand_all):
                    d_sold = sold_df[sold_df['Dealer'] == dealer_name]
                    missed_threshold_local = max(avg_v, min_visitors) if not sold_df.empty else min_visitors
                    d_missed = df[(df['Dealer'] == dealer_name) & (df['Sold_Status'] == 'Available') & (df['Category'].str.contains('VDP', na=False)) & (df['Attributed Unique Visitors'] >= missed_threshold_local)]
                    
                    if d_sold.empty and d_missed.empty:
                        st.info("No actionable sales or high-interest missed opportunities meet the current thresholds for this dealer.")
                        continue
                    
                    c_left, c_right = st.columns(2)
                    with c_left:
                        if not d_sold.empty:
                            d_sold_models = d_sold.copy()
                            d_sold_models['Model_Only'] = d_sold_models['Vehicle Name'].apply(normalize_model)
                            d_m_counts = d_sold_models['Model_Only'].value_counts().reset_index()
                            d_m_counts.columns = ['Make/Model', 'Units Sold']
                            st.markdown("**🏆 Top Sold Models**")
                            st.dataframe(d_m_counts.head(5), use_container_width=True, hide_index=True)
                    with c_right:
                        if not d_missed.empty:
                            d_missed_models = d_missed.copy()
                            d_missed_models['Model_Only'] = d_missed_models['Vehicle Name'].apply(normalize_model)
                            d_m_counts = d_missed_models['Model_Only'].value_counts().reset_index()
                            d_m_counts.columns = ['Make/Model', 'Missed Count']
                            st.markdown("**⚠️ Top Missed Models**")
                            st.dataframe(d_m_counts.head(5), use_container_width=True, hide_index=True)
                    
                    if not d_sold.empty:
                        st.markdown("**📄 Top Sold Units (Detail)**")
                        st.dataframe(d_sold[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].sort_values('Attributed Unique Visitors', ascending=False).head(10), column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open")}, use_container_width=True, hide_index=True)
                    
                    if not d_missed.empty:
                        st.markdown("**👀 Missed Opportunities (Detail)**")
                        st.dataframe(d_missed[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].sort_values('Attributed Unique Visitors', ascending=False).head(10), column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open")}, use_container_width=True, hide_index=True)


        st.divider()

    t1, t2 = st.columns(2)
    with t1:
        if not sold_df.empty:
            sold_models = sold_df.copy()
            sold_models['Model_Only'] = sold_models['Vehicle Name'].apply(normalize_model)
            model_counts = sold_models['Model_Only'].value_counts().reset_index()
            model_counts.columns = ['Make/Model', 'Units Sold']
            top_models = model_counts[model_counts['Units Sold'] > 1].head(10)
            if not top_models.empty:
                st.subheader("🏆 Top Sold Models (Aggregated > 1 Unit)")
                st.dataframe(top_models, use_container_width=True, hide_index=True)
        else:
            st.info("No sales identified.")

    with t2:
        if not missed_df.empty:
            missed_models = missed_df.copy()
            missed_models['Model_Only'] = missed_models['Vehicle Name'].apply(normalize_model)
            missed_counts = missed_models['Model_Only'].value_counts().reset_index()
            missed_counts.columns = ['Make/Model', 'Missed Count']
            top_missed = missed_counts[missed_counts['Missed Count'] > 1].head(10)
            if not top_missed.empty:
                st.subheader("⚠️ Top Missed Models (Aggregated > 1 Unit)")
                st.dataframe(top_missed, use_container_width=True, hide_index=True)
        else:
            st.info("No missed opportunities identified.")

    st.divider()

    if not sold_df.empty:
        st.subheader("📄 Top Sold Units (Most Campaign Visits)")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        display_sold = top_sold[['Dealer', 'Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
        display_sold.index += 1
        st.dataframe(display_sold, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    if not missed_df.empty:
        st.write("") 
        st.subheader("👀 Missed Opportunities (Detail)")
        display_missed = missed_df[['Dealer', 'Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].sort_values('Attributed Unique Visitors', ascending=False).head(10).reset_index(drop=True)
        display_missed.index += 1
        st.dataframe(display_missed, column_config={"Page Url": st.column_config.LinkColumn("Link", display_text="Open"), "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")}, use_container_width=True)

    st.divider()
    st.markdown("### 📥 Export Reports")
    include_dealer_details = False
    with st.container(border=True):
        st.markdown("**Include in the PDF & PowerPoint**")
        xo1, xo2 = st.columns(2)
        with xo1:
            include_missed_in_pdf = st.toggle("👀 Missed Opportunities (Watch List)", value=True)
        with xo2:
            if domain_count > 1 and dealer_group_export is not None:
                _n_dd = int(len(dealer_group_export))
                include_dealer_details = st.toggle(
                    f"🏢 Dealer Deep Dives (+{_n_dd} store pages)", value=False,
                    help="Adds a page per store: top sold and missed models, top sold units "
                         "and the watch list for that rooftop.")
    st.write("")

    metrics_bundle = {'units_sold': m_units, 'rev_sold': m_rev, 'pipeline': m_pipe, 'ltb': f"{m_ltb:.1f}", 'new_ltb': f"{new_ltb:.1f}", 'used_ltb': f"{used_ltb:.1f}", 'min_visitors': min_visitors, 'context': report_ctx,
                      'vdp_mode': vdp_mode, 'he_threshold': he_threshold, 'high_engagement': he,
                      'new_viewed': len(new_vdp_all), 'used_viewed': len(used_vdp_all)}
    export_missed_df = missed_df if not sold_df.empty else pd.DataFrame()
    _rid = st.session_state.current_report_id

    # The PDF and deck are built when the button is clicked, not on every
    # rerun, so the High-Engagement slider stays instant. (Older Streamlit
    # without deferred downloads falls back to building them up front.)
    def _chart_images():
        imgs = build_summary_chart_images(df, sold_df)
        if dealer_group_export is not None and not dealer_group_export.empty:
            imgs.update(build_group_chart_images(dealer_group_export))
        imgs.update(build_high_engagement_images(he))
        return imgs

    def _make_pdf():
        return create_pdf_report(df, sold_df, metrics_bundle, export_missed_df, include_missed_in_pdf,
                                 dealer_group_export, include_dealer_details,
                                 chart_images=_chart_images(), report_title=_rid)

    def _make_pptx():
        return build_pptx_report(df, sold_df, metrics_bundle, _chart_images(), _rid,
                                 missed_df=export_missed_df,
                                 dealer_group=dealer_group_export,
                                 include_dealer_details=include_dealer_details,
                                 include_missed=include_missed_in_pdf)

    _deferred = 'callable' in (st.download_button.__doc__ or '')
    try:
        import importlib.util as _ilu
        _pptx_ok = _ilu.find_spec('pptx') is not None
    except Exception:
        _pptx_ok = True

    ex1, ex2, ex3, ex4 = st.columns(4)
    with ex1:
        st.download_button("📥 Download PDF Summary", data=_make_pdf if _deferred else _make_pdf(),
                           file_name=f"{_rid}_Summary.pdf", mime="application/pdf")
    with ex2:
        _pptx_data = (_make_pptx if _deferred else _make_pptx()) if _pptx_ok else None
        if _pptx_data:
            st.download_button("📥 Download PowerPoint", data=_pptx_data,
                               file_name=f"{_rid}_Summary.pptx",
                               mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        else:
            st.button("📥 Download PowerPoint", disabled=True,
                      help="PowerPoint export needs the python-pptx package on the host.")
    with ex3:
        _sold_out = sold_df[['Dealer', 'Vehicle Name', 'VIN', 'Page Url', 'Attributed Unique Visitors']].copy()
        if he is not None:
            _sold_out[f"{he['threshold']}+ Visits"] = (
                _sold_out['Attributed Unique Visitors'] >= he['threshold']).map({True: 'Yes', False: 'No'})
        st.download_button("📥 Download Sold List (CSV)", _sold_out.to_csv(index=False), f"{_rid}_Sold.csv", "text/csv")
    with ex4:
        st.download_button("📥 Download Full Analysis (CSV)", df.to_csv(index=False), f"{_rid}_Full_Analysis.csv", "text/csv")

    # Tucked away on purpose: this file is for the attribution report builder,
    # not part of a rep's normal workflow. Built from the same frames as the
    # deck so the two can never disagree.
    if is_admin():
      with st.expander("🧩 Data export (for attribution reporting)", expanded=False):
          facts_bytes = facts_name = None
          try:
              facts_payload = build_facts_json(df, vdp_df, sold_df, export_missed_df,
                                               metrics_bundle, _rid,
                                               dealer_group=dealer_group_export,
                                               report_ctx=report_ctx)
              if facts_payload:
                  facts_bytes = json.dumps(facts_payload, indent=2).encode('utf-8')
                  facts_name = facts_filename(_rid, facts_payload)
          except Exception as _fe:
              st.caption(f"Facts export unavailable for this report ({type(_fe).__name__}).")
          if facts_bytes:
              st.caption("Structured version of this report's figures, for combining with "
                         "website attribution and Polk data.")
              st.download_button("Download facts (.json)", data=facts_bytes,
                                 file_name=facts_name, mime="application/json")
          else:
              st.caption("Select the **report month** under 🗓️ Report Details in the "
                         "sidebar to enable this export — the reporting period is needed "
                         "to line this analysis up against website and Polk data.")

    st.divider()
    
    with st.expander("ℹ️ Glossary & Guide: How to read this report"):
        st.markdown("""
        ### **Definitions & Insights**
        *(These match the Glossary & Methodology section in your PDF and PowerPoint exports.)*

        **1. Viewed Vehicles Sold**
        Vehicles our campaign audience viewed on the dealer's site that have since left its live inventory. This shows the campaign sending **qualified shoppers into the pipeline** and the pace that inventory is moving — it indicates **influence and sales velocity, not purchases made by our visitors.**

        **2. Est. Value Sold, Est. Total Value Viewed & Value Still on the Lot**
        Directional, data-driven value estimates — not exact transaction prices.
        * **New cars:** Base MSRP for the specific model.
        * **Used cars:** Base MSRP depreciated by age — **15% for year one, 10% for each following year.**
        * **Est. Total Value Viewed** is the estimated value of *every* vehicle our audience viewed — sold, still listed, and unconfirmed. It sizes the in-market demand the campaign reached.
        * **Est. Value Still on the Lot** is the part of that total that hasn't sold — viewed vehicles still listed for sale (unconfirmed vehicles left out).
        * *Excludes trim levels, options, and dealer markups — don't quote these as deal values.*

        **3. Look-to-Book Ratio**
        How quickly the inventory our audience engaged with is moving, split New vs. Used.
        * *Formula:* `Viewed vehicles sold ÷ all viewed vehicles`
        * *Tip: the High-Engagement Filter shows whether the vehicles our audience came back to most are selling faster. When they are, the comparison appears on screen and in the exports; when they aren't, it stays out.*

        **3b. High-Engagement Sales**
        Viewed vehicles sold that drew the threshold number of campaign visits or more (5+ by default) — the sales where our audience's engagement was deepest. In the default **Highlight** mode it's a conservative subset shown *alongside* the full report; **Filter** mode limits the whole report to those vehicles instead.

        **4. Traffic Mix**
        Where audiences navigated on the dealer's site: VDPs, Service, Search, Incentives/Offers, Homepage. **VDP visits are the high-intent inventory shoppers** — the audience most likely to buy.

        **5. Top Sold & Missed Opportunities ("The Watch List")**
        * **Top Sold:** the vehicles that received the most exposure and subsequently sold. The aggregated *Top Models* view rolls all trims of the same model into one line (six Silverado 1500s = one row); the detail tables keep full trim-level names.
        * **Missed Opportunities:** active vehicles receiving **above-average traffic** that haven't sold yet. Position this as a consultative Watch List — audit those VDPs for missing photos, "Call for Price" buttons, or pricing outliers.

        **6. Auto Group / Tier 2 Breakdown**
        Multi-dealer reports aggregate store-by-store automatically. Dealerships with general traffic but **0 VDPs viewed** are filtered out to keep the table focused on inventory shoppers.

        **7. How Inventory Is Checked (Methodology)**
        The tool scans the dealer's website in **real time** — results reflect inventory status at the moment the report runs, not a historical snapshot. If a dealer's site blocks scanning, the tool **automatically falls back to live national market inventory** (and remembers recent answers for about a week), so manual fixes are rarely needed.
        * *Trust but verify:* before presenting, click a few **Top Sold** links (they should 404 or redirect to the search page) and a few **Watch List** links (they should load a live vehicle page).
        """)

else:
    st.info("👈 Upload a CSV in the sidebar to begin analysis.")
    st.markdown("---")
    st.markdown("### 🆕 What's New")
    st.markdown(
        "- **⭐ High-Engagement Sales.** Keep the full report and call out the vehicles our "
        "audience came back to most (5+ visits by default) right alongside it. Or choose "
        "**Filter report** for your most discerning dealers, or turn it **Off**. *If you used "
        "to raise the VDP filter, your headline numbers will look bigger - that's the full picture.*\n"
        "- **📊 Client-ready PowerPoint and PDF.** Premion-styled, built to sit alongside your "
        "website attribution deck, with matching sections and a glossary. Group reports add a "
        "store scoreboard and an optional page per store.\n"
        "- **🔓 Blocked dealer sites just work.** If a site blocks our scanner, the tool checks "
        "live market inventory on its own. No manual fixes needed.\n"
        "- **🗓️ Report Details.** Add the campaign month and unique visitors (above the Build "
        "button, or on the report) to show the lookback window and pages per visitor.\n"
        "- **🏷️ Clearer labels.** \"Viewed Vehicles Sold\" and dealer names pulled from their "
        "own websites. Models now group across trims.")
    st.caption("Questions or something look off? Send them my way.")
