import streamlit as st
import pandas as pd
import requests
import re
import concurrent.futures
import matplotlib.pyplot as plt
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
st.set_page_config(page_title="Universal Auto-Sales Intelligence", layout="wide")

# --- PASSWORD PROTECTION ---
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

# --- THE "AUTOMOTIVE EXPERT" UNIVERSAL LOGIC ---

def extract_car_info_universal(url):
    """
    Truly Universal: Finds the 4-digit year anywhere in the URL string.
    """
    if not isinstance(url, str): return None, None
    url_clean = url.lower().strip()
    
    # Ignore search/inventory results pages
    if any(x in url_clean.split('/')[-1] for x in ['search', 'inventory', 'all', 'index', '.aspx']):
        return None, None

    # Find a 4-digit year (19xx or 20xx) anywhere in the URL
    year_match = re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:$|[^0-9])', url_clean)
    if not year_match:
        return None, None
    
    year = year_match.group(1)
    
    # Try to find the word immediately following the year (usually the Make)
    make_match = re.search(rf'{year}[- ]([a-z0-9]+)', url_clean)
    make = make_match.group(1).title() if make_match else "Vehicle"
    
    return year, make

def check_single_url(url):
    """
    Checks if a vehicle is sold using Redirects, Title Mismatches, and 404s.
    """
    year, make = extract_car_info_universal(url)
    if not year:
        return "N/A (Not a VDP)"

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        # Follow redirects is crucial
        response = requests.get(url, headers=headers, timeout=7, allow_redirects=True)
        
        final_url = response.url.lower()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_title = soup.title.string.strip().lower() if soup.title else ""
        page_text = response.text.lower()

        # 1. Check for Explicit "Sold" indicators
        sold_phrases = ["vehicle not found", "no longer available", "sold", "similar vehicles", "sorry"]
        if any(phrase in page_text for phrase in ["vehicle not found", "no longer available"]):
            return "SOLD (Error Message)"
        
        if "sold" in page_title:
            return "SOLD (Title Tag)"

        # 2. Check for Redirect to Search/Inventory (Platform Agnostic)
        # If the URL changes to a search results page, the specific car is gone.
        search_indicators = ['search', 'inventory', 'all-inventory', 'used-inventory', 'results', '.aspx']
        if any(ind in final_url for ind in search_indicators) and url.lower().split('?')[0] not in final_url:
            return "SOLD (Redirected)"

        # 3. Check for Title Mismatch (The Soft Redirect)
        # If the URL says "2023" but the resulting page title is generic, it's sold.
        if year not in page_title:
            if len(page_title) > 5: # Ignore empty titles
                return "SOLD (Title Mismatch)"

        return "Available"

    except:
        return "Error (Connection)"

def get_display_category(url):
    url = str(url).lower().strip()
    if url.endswith('.com/') or url.endswith('.com'): return 'Homepage'
    if any(x in url for x in ['search', 'inventory']):
        if 'new' in url: return 'New Car Search'
        if 'used' in url or 'preowned' in url: return 'Used Car Search'
        return 'General Search'
    if any(x in url for x in ['service', 'parts', 'collision', 'appointment']): return 'Service'
    if re.search(r'(?:19|20)\d{2}', url): return 'VDP'
    return 'Other'

def clean_vehicle_name(url):
    """Creates a readable name regardless of URL structure."""
    url_path = urlparse(url).path
    # Find year and everything after it
    match = re.search(r'((?:19|20)\d{2}.*)', url_path)
    if match:
        name = match.group(1).replace('/', '-').replace('.htm', '').replace('.html', '').replace('-', ' ')
        # Clean up common suffixes
        name = re.sub(r' Baltimore.*| Ephrata.*| [a-z0-9]{20,}', '', name, flags=re.IGNORECASE)
        return name.title().strip()
    return "Unknown Vehicle"

# --- MAIN APP UI ---
st.title("🚗 Universal Sales Intelligence Agent")
st.markdown("Automotive-grade analysis that works across all dealership website platforms.")

uploaded_file = st.file_uploader("Upload Raw Traffic CSV", type=['csv'])

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    st.info(f"Processing {len(df)} lines. Analyzing VDPs for Sold status...")
    
    progress_bar = st.progress(0)
    urls = df['Page Url'].tolist()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        status_results = list(executor.map(check_single_url, urls))
        
    df['Sold_Status'] = status_results
    df['Category'] = df['Page Url'].apply(get_display_category)
    df['Vehicle Name'] = df['Page Url'].apply(clean_vehicle_name)
    
    # Determine New/Used
    def get_type(url):
        year_match = re.search(r'(202[5-7])', url)
        if year_match: return 'New'
        return 'Used'
    df['Type'] = df['Page Url'].apply(get_type)
    
    df['Is Sold'] = df['Sold_Status'].str.startswith('SOLD')
    
    st.success("Report Generated!")
    st.divider()
    
    # --- DASHBOARD ---
    sold_df = df[df['Is Sold']]
    vdp_df = df[df['Category'] == 'VDP']
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Cars Sold", len(sold_df))
    c2.metric("New Sold", len(sold_df[sold_df['Type'] == 'New']))
    c3.metric("Used Sold", len(sold_df[sold_df['Type'] == 'Used']))
    c4.metric("Look-to-Book", f"{(len(sold_df)/len(vdp_df)*100 if len(vdp_df)>0 else 0):.1f}%")
    
    # --- VISUALS ---
    col_chart1, col_chart2 = st.columns(2)
    with col_chart1:
        st.markdown("**Traffic Mix**")
        traffic = df[df['Category'] != 'Other'].groupby('Category')['Attributed Unique Visitors'].sum()
        st.bar_chart(traffic)
    with col_chart2:
        st.markdown("**Sales Mix**")
        if len(sold_df) > 0:
            mix = sold_df['Type'].value_counts()
            fig, ax = plt.subplots()
            ax.pie(mix, labels=mix.index, autopct='%1.1f%%', colors=['#66b3ff', '#99ff99'])
            st.pyplot(fig)

    # --- TABLES ---
    t1, t2 = st.columns(2)
    with t1:
        st.subheader("🏆 Top Sold Units")
        top_sold = sold_df.sort_values('Attributed Unique Visitors', ascending=False).head(10)
        top_sold_disp = top_sold[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        top_sold_disp.index += 1
        st.dataframe(top_sold_disp, use_container_width=True)
    with t2:
        st.subheader("⚠️ High Interest / Unsold")
        avg_s = sold_df['Attributed Unique Visitors'].mean() if len(sold_df)>0 else 0
        missed = df[(~df['Is Sold']) & (df['Category'] == 'VDP') & (df['Attributed Unique Visitors'] >= avg_s)].sort_values('Attributed Unique Visitors', ascending=False).head(10)
        missed_disp = missed[['Vehicle Name', 'Type', 'Attributed Unique Visitors']].reset_index(drop=True)
        missed_disp.index += 1
        st.dataframe(missed_disp, use_container_width=True)

    st.download_button("Download Report", df.to_csv(index=False), "Full_Report.csv", "text/csv")
