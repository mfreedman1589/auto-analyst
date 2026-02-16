import streamlit as st
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
    """Finds the 4-digit year anchor anywhere in the URL."""
    match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', str(url))
    return match.group(1) if match else None

def extract_vin(url):
    """Extracts the VIN or unique ID from the end of the URL path."""
    match = re.search(r'([a-zA-Z0-9]{10,})(?:\.htm|\.html|$|\?)', str(url))
    return match.group(1).upper() if match else "N/A"

def check_universal_status(url, session):
    """Simplified Universal Logic with High-Speed optimizations."""
    year = get_year(url)
    if not year: return "N/A"
    
    try:
        # Fast 3-second timeout to prevent hanging
        response = session.get(url, timeout=3, allow_redirects=True)
        
        # Quick checks on the response
        final_url = response.url.lower().rstrip('/')
        orig_url = url.lower().split('?')[0].rstrip('/')
        
        # 1. HARD REDIRECT check (Fastest check)
        search_path = ['search', 'inventory', 'results', '.aspx', 'all-inventory', 'index.htm']
        if any(x in final_url for x in search_path) and orig_url not in final_url:
            return "SOLD (Hard Redirect)"

        # Only parse HTML if necessary (saves CPU)
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        
        # 2. SOFT REDIRECT: If Year is in URL but NOT in Title, it's SOLD.
        if year not in page_title and len(page_title) > 5:
            return "SOLD (Soft Redirect)"

        # 3. CONTENT CHECK
        page_text = response.text.lower()
        if "vehicle not found" in page_text or "no longer available" in page_text:
            return "SOLD (Content)"

        return "Available"
    except:
        return "Available" # Default to available if connection fails

def clean_name_universal(url):
    """Universal Name Extractor: Year + Brand + Model."""
    year = get_year(url)
    if not year: return "Unknown Vehicle"
    
    path = urlparse(url).path.lower()
    brands = ['Jeep', 'Ford', 'Gmc', 'Toyota', 'Dodge', 'Ram', 'Chrysler', 'Chevrolet', 'Honda', 'Nissan', 'Hyundai', 'Kia', 'Bmw', 'Lexus', 'Volvo', 'Volkswagen', 'Subaru', 'Mazda', 'Mercedes', 'Audi', 'Cadillac', 'Buick', 'Acura', 'Infiniti', 'Lincoln', 'Land Rover', 'Jaguar', 'Porsche', 'Mini']
    make = ""
    for b in brands:
        if b.lower() in path:
            make = b
            break
            
    rest = url.split(year)[-1].replace('/', ' ').replace('-', ' ').replace('+', ' ').replace('.htm', '').replace('.html', '')
    tokens = rest.split()
    junk = ['Baltimore', 'Ephrata', 'Md', 'Maryland', 'Heritage', 'Twin', 'Pine', 'Wholesale', 'New', 'Used', 'Preowned', 'Inventory', 'Parts', 'Service', 'Finance', 'Global', 'Incentives', 'Offers']
    
    clean_tokens = []
    for t in tokens:
        if len(t) > 10 and any(c.isdigit() for c in t): continue 
        if t.title() in junk or t.title() == make: continue 
        clean_tokens.append(t)
        
    return f"{year} {make} {' '.join(clean_tokens)}".title().strip()

def categorize(u):
    u = str(u).lower()
    if u.endswith('.com/') or u.endswith('.com'): return 'Homepage'
    if any(x in u for x in ['search', 'inventory']):
        if 'new' in u: return 'New Car Search'
        if 'used' in u or 'preowned' in u: return 'Used Car Search'
        return 'General Search'
    if any(x in u for x in ['service', 'parts', 'collision', 'appointment', 'maintenance']): return 'Service'
    if get_year(u): return 'VDP'
    return 'Other'

# --- UI DASHBOARD ---
st.title("🚗 Auto-Sales Intelligence Agent")
uploaded_file = st.file_uploader("Upload Traffic Report (CSV)", type=['csv'])

if uploaded_file is not None:
    df_raw = pd.read_csv(uploaded_file)
    
    if st.button("🚀 Analyze Traffic"):
        # 1. Pre-process to identify VDPs (Smart Filtering)
        df_raw['Category'] = df_raw['Page Url'].apply(categorize)
        vdp_urls = df_raw[df_raw['Category'] == 'VDP']['Page Url'].tolist()
        
        st.info(f"Identified {len(vdp_urls)} Vehicles out of {len(df_raw)} total links. Scanning Vehicles now...")
        progress_bar = st.progress(0)
        
        # 2. Setup High-Speed Session
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0'})
        
        # 3. Scan ONLY VDPs
        vdp_results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            future_to_url = {executor.submit(check_universal_status, url, session): url for url in vdp_urls}
            for i, future in enumerate(concurrent.futures.as_completed(future_to_url)):
                url = future_to_url[future]
                vdp_results[url] = future.result()
                progress_bar.progress((i + 1) / len(vdp_urls))

        # 4. Map results back to the main DataFrame
        # Non-VDPs get "N/A" status automatically
        df_raw['Sold_Status'] = df_raw['Page Url'].map(vdp_results).fillna('N/A')
        
        # 5. Final Data Processing
        df = df_raw.copy()
        df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
        df['Vehicle Name'] = df['Page Url'].apply(clean_name_universal)
        df['VIN'] = df['Page Url'].apply(extract_vin)
        df['Type'] = df['Page Url'].apply(lambda x: 'New' if re.search(r'202[5-7]', str(x)) else 'Used')
        
        # --- METRICS ---
        sold_df = df[df['Is Sold']]
        vdp_df = df[df['Category'] == 'VDP']
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Sold", len(sold_df))
        c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
        c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
        c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")

        st.divider()
        
        # --- VISUALS ---
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Traffic Mix (Total Visitors)**")
            traffic = df.groupby('Category')['Attributed Unique Visitors'].sum().sort_values(ascending=False)
            st.bar_chart(traffic)
        with col2:
            st.markdown("**Sales Mix**")
            if not sold_df.empty:
                fig, ax = plt.subplots()
                sold_df['Type'].value_counts().plot.pie(autopct='%1.1f%%', ax=ax, colors=['#4F81BD', '#C0504D'])
                ax.set_ylabel('')
                st.pyplot(fig)

        # --- TABLES (Fixed Index & Limits) ---
        t1, t2 = st.columns(2)
        
        with t1:
            st.subheader("🏆 Top Sold Units")
            if not sold_df.empty:
                # Limit to 10
                top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
                
                # Create display DF and fix index to start at 1
                display_sold = top_sold[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
                display_sold.index += 1
                
                st.dataframe(
                    display_sold,
                    column_config={
                        "Page Url": st.column_config.LinkColumn("Link", display_text="Open Link"),
                        "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")
                    },
                    use_container_width=True
                )
            else:
                st.info("No sales identified.")
                
        with t2:
            st.subheader("⚠️ Missed Opportunities")
            if not sold_df.empty:
                avg_v = sold_df['Attributed Unique Visitors'].mean()
                missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_v)]
                # Limit to 10
                missed = missed.sort_values('Attributed Unique Visitors', ascending=False).head(10)
                
                # Create display DF and fix index to start at 1
                display_missed = missed[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].reset_index(drop=True)
                display_missed.index += 1
                
                st.dataframe(
                    display_missed,
                    column_config={
                        "Page Url": st.column_config.LinkColumn("Link", display_text="Open Link"),
                        "Attributed Unique Visitors": st.column_config.NumberColumn("Visitors")
                    },
                    use_container_width=True
                )
            else:
                st.info("Data pending.")

        # --- EXPORT ---
        st.divider()
        ex1, ex2 = st.columns(2)
        with ex1:
            st.download_button("📥 Download Sold List (CSV)", 
                               sold_df[['Vehicle Name', 'Type', 'VIN', 'Attributed Unique Visitors', 'Page Url']].to_csv(index=False), 
                               "Sold_Vehicles_Report.csv", "text/csv")
        with ex2:
            st.download_button("📥 Download Full Analysis (CSV)", 
                               df.to_csv(index=False), 
                               "Full_Market_Analysis.csv", "text/csv")
