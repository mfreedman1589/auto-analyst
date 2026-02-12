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

# --- THE ENGINE ---

def get_year(url):
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def extract_vin(url):
    """Universal VIN/ID extractor: Finds the long alphanumeric code at the end of the URL."""
    # Matches strings like '1GTUUGEL2PZ290300' or 'a9278301ac183886ae6bc8d95a9b05db'
    match = re.search(r'([a-zA-Z0-9]{10,})(?:\.htm|\.html|$|\?)', str(url))
    return match.group(1).upper() if match else "N/A"

def check_universal_status(url, session):
    year = get_year(url)
    if not year: return "N/A"
    try:
        response = session.get(url, timeout=6, allow_redirects=True)
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        # 1. THE UNIVERSAL RULE: If the Year is in the URL but NOT the Title, it's SOLD.
        if year not in page_title and len(page_title) > 5:
            return "SOLD (Soft Redirect)"

        # 2. HARD REDIRECT check
        final_url = response.url.lower().rstrip('/')
        orig_url = url.lower().split('?')[0].rstrip('/')
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'index.htm']
        if any(x in final_url for x in search_path) and orig_url not in final_url:
            return "SOLD (Hard Redirect)"

        # 3. CONTENT CHECK
        if any(x in response.text.lower() for x in ["vehicle not found", "no longer available"]):
            return "SOLD (Content)"

        return "Available"
    except:
        return "Available"

def clean_name_universal(url):
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    
    path = urlparse(url).path.lower()
    brands = ['Jeep', 'Ford', 'Gmc', 'Toyota', 'Dodge', 'Ram', 'Chrysler', 'Chevrolet', 'Honda', 'Nissan', 'Hyundai', 'Kia']
    make = ""
    for b in brands:
        if b.lower() in path:
            make = b
            break
            
    rest = url.split(year)[-1].replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    tokens = rest.split()
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory']
    
    clean_tokens = []
    for t in tokens:
        if len(t) > 10 and any(c.isdigit() for c in t): continue 
        if t.title() in junk or t.title() == make: continue 
        clean_tokens.append(t)
        
    return f"{year} {make} {' '.join(clean_tokens)}".title().strip()

# --- MAIN DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent")
uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    
    # Progress Bar UI
    st.info(f"Scanning {len(df)} URLs for live status...")
    progress_bar = st.progress(0)
    
    # Analysis Loop
    urls = df['Page Url'].tolist()
    status_results = [None] * len(urls)
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_index = {executor.submit(check_universal_status, url, session): i for i, url in enumerate(urls)}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_index)):
            idx = future_to_index[future]
            status_results[idx] = future.result()
            # Update Progress
            progress_bar.progress((i + 1) / len(urls))

    df['Sold_Status'] = status_results
    df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
    df['Vehicle Name'] = df['Page Url'].apply(clean_name_universal)
    df['VIN'] = df['Page Url'].apply(extract_vin)
    df['Type'] = df['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
    df['Category'] = df['Page Url'].apply(lambda u: 'Homepage' if str(u).endswith('.com/') else ('Search' if 'search' in str(u).lower() or 'inventory' in str(u).lower() else ('VDP' if get_year(u) else 'Other')))

    # Metrics
    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

    st.divider()
    
    # Visuals
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Traffic Mix (Total Visitors)**")
        st.bar_chart(df.groupby('Category')['Attributed Unique Visitors'].sum().sort_values(ascending=False))
    with col2:
        st.markdown("**Sales Mix**")
        if not sold_df.empty:
            fig, ax = plt.subplots()
            sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
            ax.set_ylabel('')
            st.pyplot(fig)

    # Tables - No Aggregation (Detailed View)
    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units (Individual)")
        if not sold_df.empty:
            # Show individual vehicles, sorted by traffic
            top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(15)
            top_sold_disp = top_sold[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors']].reset_index(drop=True)
            top_sold_disp.index += 1
            st.dataframe(top_sold_disp, use_container_width=True)
        else:
            st.info("No sales identified.")
            
    with t2:
        st.subheader("⚠️ Missed Opportunities")
        if not sold_df.empty:
            avg_v = sold_df['Attributed Unique Visitors'].mean()
            missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)]
            missed_disp = missed.sort_values('Attributed Unique Visitors', ascending=False).head(15)[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors']].reset_index(drop=True)
            missed_disp.index += 1
            st.dataframe(missed_disp, use_container_width=True)

    # Export Section
    st.divider()
    ex1, ex2 = st.columns(2)
    with ex1:
        st.download_button("📥 Download Full Sold List (CSV)", 
                           sold_df[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors']].to_csv(index=False), 
                           "Sold_Vehicles_List.csv", "text/csv")
    with ex2:
        st.download_button("📥 Download Full Analysis (CSV)", 
                           df.to_csv(index=False), 
                           "Full_Traffic_Analysis.csv", "text/csv")
