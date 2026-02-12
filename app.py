import streamlit as st
import pandas as pd
import requests
import re
import concurrent.futures
import matplotlib.pyplot as plt
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
st.set_page_config(page_title="Auto-Sales Intelligence Agent", layout="wide")

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

# --- UNIVERSAL ENGINE ---

def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def check_universal_status(url, session):
    """Universal Sold Check: Focuses on URL Behavior & Title Pivots."""
    year = get_year(url)
    if not year: return "N/A"
    
    try:
        response = session.get(url, timeout=5, allow_redirects=True)
        final_url = response.url.lower().rstrip('/')
        orig_url = url.lower().split('?')[0].rstrip('/')
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        # Logic 1: Hard Redirect to search/inventory
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'searchall']
        if any(x in final_url for x in search_path) and orig_url not in final_url:
            return "SOLD (Redirected)"

        # Logic 2: Hard Content Error
        if any(x in response.text.lower() for x in ["vehicle not found", "no longer available", "this vehicle has been sold"]):
            return "SOLD (Content)"
            
        # Logic 3: Title Pivot (Year in URL is missing from Title)
        generic_terms = ['inventory', 'search', 'cars for sale', 'results', 'dealership']
        if year not in page_title and any(x in page_title for x in generic_terms):
            return "SOLD (Title Pivot)"

        return "Available"
    except:
        return "Available"

def clean_name_universal(url):
    """Universal Vehicle Name Extractor: Year + Model (Stripping VINs/Junk)."""
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    
    # Take everything after the year
    parts = url.split(year)
    rest = parts[-1].replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    
    tokens = rest.split()
    # VIN-Killer: Remove words > 10 chars with numbers
    # Junk-Killer: Remove dealer names/cities
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory', 'Shop']
    
    final_tokens = []
    for t in tokens:
        if len(t) > 10 and any(c.isdigit() for c in t): continue
        if t.title() in junk: continue
        final_tokens.append(t)
        
    return f"{year} {' '.join(final_tokens)}".title().strip()

@st.cache_data(show_spinner=False)
def analyze_data(df_input):
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
    urls = df_input['Page Url'].tolist()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        df_input['Sold_Status'] = list(executor.map(lambda u: check_universal_status(u, session), urls))
    
    df_input['Is Sold'] = df_input['Sold_Status'].str.startswith('SOLD')
    df_input['Vehicle Name'] = df_input['Page Url'].apply(clean_name_universal)
    
    # Categorization
    def categorize(u):
        u = str(u).lower()
        if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
        if any(x in u for x in ['search', 'inventory']):
            if 'new' in u: return 'New Car Search'
            if 'used' in u or 'preowned' in u: return 'Used Car Search'
            return 'General Search'
        if any(x in u for x in ['service', 'parts', 'collision', 'appointment']): return 'Service'
        if get_year(u): return 'VDP'
        return 'Other'
    
    df_input['Category'] = df_input['Page Url'].apply(categorize)
    
    # Refined New/Used Logic: Folder priority, then Year
    def classify(u):
        u_low = str(u).lower()
        year = get_year(u)
        if 'used' in u_low or 'preowned' in u_low: return 'Used'
        if 'new' in u_low: return 'New'
        if year and int(year) >= 2025: return 'New'
        return 'Used'
        
    df_input['Type'] = df_input['Page Url'].apply(classify)
    return df_input

# --- UI ---
st.title("🚗 Auto-Sales Intelligence Agent")

uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df_raw = pd.read_csv(uploaded_file)
    with st.spinner("Analyzing live inventory..."):
        df = analyze_data(df_raw)

    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    # Metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Cars Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Traffic Mix (Total Visitors)**")
        st.bar_chart(df.groupby('Category')['Attributed Unique Visitors'].sum().sort_values(ascending=False))
    with col2:
        st.markdown("**Sales Mix (New vs Used)**")
        if not sold_df.empty:
            fig, ax = plt.subplots()
            sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
            ax.set_ylabel('')
            st.pyplot(fig)

    # Tables - Now Aggregated by Model
    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units (By Model)")
        if not sold_df.empty:
            # Aggregate traffic by cleaned name and type
            top_sold = sold_df.groupby(['Vehicle Name', 'Type'])['Attributed Unique Visitors'].sum().reset_index()
            top_sold = top_sold.sort_values('Attributed Unique Visitors', ascending=False).head(10)
            top_sold.index = range(1, len(top_sold) + 1)
            st.dataframe(top_sold, use_container_width=True)
        else:
            st.info("No sales detected.")
            
    with t2:
        st.subheader("⚠️ Missed Opportunities")
        if not sold_df.empty:
            avg_v = sold_df['Attributed Unique Visitors'].mean()
            # Aggregate missed opportunities
            available_vdp = df[(~df['Is Sold']) & (df['Category'] == 'VDP')]
            missed = available_vdp.groupby(['Vehicle Name', 'Type'])['Attributed Unique Visitors'].sum().reset_index()
            missed = missed[missed['Attributed Unique Visitors'] >= avg_v]
            missed = missed.sort_values('Attributed Unique Visitors', ascending=False).head(10)
            missed.index = range(1, len(missed) + 1)
            st.dataframe(missed, use_container_width=True)
        else:
            st.info("Awaiting sales data for benchmark.")

    st.divider()
    st.download_button("📥 Download Analysis CSV", df.to_csv(index=False), "Sales_Analysis.csv", "text/csv")
