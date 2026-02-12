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
    """The Universal Sold Check: Surgical accuracy at high speed."""
    year = get_year(url)
    if not year: return "N/A"
    
    try:
        # 5-second timeout per URL
        response = session.get(url, timeout=5, allow_redirects=True)
        final_url = response.url.lower().rstrip('/')
        orig_url = url.lower().split('?')[0].rstrip('/')
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        # Behavior 1: LANDED ON SEARCH (Hard Redirect)
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'index.htm', 'index.html']
        if any(x in final_url for x in search_path) and orig_url not in final_url:
            return "SOLD (Redirected)"

        # Behavior 2: TITLE PIVOT (Soft Redirect)
        # If the car is gone, titles usually change to generic inventory terms.
        generic_terms = ['inventory', 'search', 'cars for sale', 'results', 'dealership']
        if year not in page_title and any(x in page_title for x in generic_terms):
            return "SOLD (Title Pivot)"
            
        # Behavior 3: HARD CONTENT ERROR
        if any(x in response.text.lower() for x in ["vehicle not found", "no longer available"]):
            return "SOLD (Content)"

        return "Available"
    except:
        return "Available" # Assume available if the site times out

def clean_name_universal(url):
    """Cleans names by removing VINs, dealer cities, and system codes."""
    url_path = urlparse(url).path
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        raw = match.group(1).replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '')
        tokens = raw.split()
        # VIN-Killer: Remove any token > 10 chars that contains a digit
        clean = [t for t in tokens if not (len(t) > 10 and any(c.isdigit() for c in t))]
        # Junk-Killer: Remove dealership names and locations
        junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Twin', 'Pine', 'Heritage', 'Used', 'New', 'Wholesale', 'Inventory']
        final = [t for t in clean if t.title() not in junk]
        return " ".join(final).title().strip()
    return "Unknown Vehicle"

# --- MAIN APP LOGIC ---
st.title("🚗 Auto-Sales Intelligence Agent")
uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    
    # --- SPEED SCANNING ENGINE ---
    if st.button("🚀 Run Sales Analysis"):
        st.info(f"Scanning {len(df)} URLs. Estimated time: {int(len(df)/10)} seconds...")
        
        progress_bar = st.progress(0)
        status_results = [None] * len(df)
        session = requests.Session()
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
        
        # Parallel Execution (50 Workers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            future_to_url = {executor.submit(check_universal_status, url, session): i 
                             for i, url in enumerate(df['Page Url'])}
            
            for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
                idx = future_to_url[future]
                status_results[idx] = future.result()
                # Update progress bar
                progress_bar.progress((i + 1) / len(df))
        
        df['Sold_Status'] = status_results
        df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
        df['Vehicle Name'] = df['Page Url'].apply(clean_name_universal)
        df['Type'] = df['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
        df['Category'] = df['Page Url'].apply(lambda u: 'Homepage' if str(u).endswith('.com/') else ('Search' if 'search' in str(u).lower() or 'inventory' in str(u).lower() else ('VDP' if get_year(u) else 'Other')))

        # --- DASHBOARD ---
        sold_df = df[df['Is Sold']]
        vdp_df = df[df['Category'] == 'VDP']
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Sold", len(sold_df))
        c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
        c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
        c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Traffic Mix (Total Visitors)**")
            st.bar_chart(df.groupby('Category')['Attributed Unique Visitors'].sum())
        with col2:
            st.markdown("**Sales Mix (New vs Used)**")
            if not sold_df.empty:
                fig, ax = plt.subplots()
                sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
                ax.set_ylabel('')
                st.pyplot(fig)

        # Tables
        t1, t2 = st.columns(2)
        with t1:
            st.subheader("🏆 Top Sold Units")
            ts = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
            ts.index += 1
            st.dataframe(ts, use_container_width=True)
        with t2:
            st.subheader("⚠️ High Interest / Unsold")
            avg_v = sold_df['Attributed Unique Visitors'].mean() if not sold_df.empty else 0
            missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)].sort_values('Attributed Unique Visitors', ascending=False).head(10)[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
            missed.index += 1
            st.dataframe(missed, use_container_width=True)

        st.divider()
        st.download_button("📥 Download Analysis CSV", df.to_csv(index=False), "Sales_Analysis.csv", "text/csv")
