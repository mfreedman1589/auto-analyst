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

# --- PASSWORD PROTECTION ---
def check_password():
    """Returns `True` if the user had the correct password."""
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False

    if st.session_state.password_correct:
        return True

    st.title("🔒 Auto-Analyst Login")
    password = st.text_input("Enter Company Password", type="password")
    
    if st.button("Log In"):
        if password == "tegna2026":  # <--- PASSWORD
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Incorrect password")
    return False

if not check_password():
    st.stop()

# --- UNIVERSAL PARSING LOGIC ---
def extract_car_info_universal(url):
    """
    Attempts to extract Year, Make from ANY dealership URL.
    """
    if not isinstance(url, str):
        return None, None
        
    url = url.lower().strip()
    
    # Skip search/index pages
    if "search" in url or "index" in url or "inventory" in url.split('/')[-1]:
        return None, None

    # Parse path to find the segment with the car info
    parsed = urlparse(url)
    path_segments = parsed.path.split('/')
    
    car_segment = None
    for segment in path_segments:
        # Check if segment starts with 4 digits (19xx or 20xx)
        if re.match(r'^(19|20)\d{2}', segment):
            car_segment = segment
            break
            
    if not car_segment:
        return None, None
        
    # Extract Year and Make
    tokens = car_segment.split('-')
    if len(tokens) >= 2:
        year = tokens[0]
        make = tokens[1]
        return year, make
        
    return None, None

def check_single_url(url):
    """
    Checks status using the Universal Logic (Title Mismatch, Redirects, Error Text).
    """
    # 1. Parse URL to get expected car info
    year, make = extract_car_info_universal(url)
    
    if not year:
        return "N/A"

    try:
        # 2. Request the page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept
