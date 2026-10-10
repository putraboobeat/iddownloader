import requests
import time
import json
import os
import subprocess
import shutil

print("Starting backend for testing...")
proc = subprocess.Popen(["python3", "app.py", "--port", "7777", "--vps"])
time.sleep(2)

API_URL = "http://localhost:7777"
# We need to fetch the index page and extract the token!
try:
    idx = requests.get(API_URL)
    token = None
    for line in idx.text.splitlines():
        if "const token=" in line:
            token = line.split("const token='")[1].split("'")[0]
            break
    if not token:
        print("TOKEN NOT FOUND")
        proc.terminate()
        exit(1)
except Exception as e:
    print("GET error", e)
    proc.terminate()
    exit(1)

headers = {"Content-Type": "application/json", "X-App-Token": token}

def run_test(name, payload):
    print(f"\n--- Testing: {name} ---")
    try:
        print("Scanning...")
        # Inspect is a GET request with query param url
        url = payload.get('url')
        res = requests.get(f"{API_URL}/inspect?url={url}", headers=headers)
        if res.status_code != 200:
            print("SCAN FAILED:", res.text)
            return False
        
        info = res.json().get('info', {})
        print(f"Scan Success. Title: {info.get('title')}")
        
        print("Starting download...")
        payload['name'] = info.get('title', 'test')
        res = requests.post(f"{API_URL}/start", json=payload, headers=headers)
        if res.status_code != 200:
            print("START FAILED:", res.text)
            return False
            
        while True:
            time.sleep(3)
            status_res = requests.get(f"{API_URL}/status", headers=headers)
            status_data = status_res.json()
            if not status_data.get('running'):
                break
                
        print("Final Status:", status_data.get('status'))
        files = status_data.get('files', [])
        print("Files created:", [f['name'] for f in files])
        return bool(files)
        
    except Exception as e:
        print("TEST EXCEPTION:", e)
        return False

# 1. Test YouTube Video
payload_yt = {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "mode": "ytdlp"}
run_test("YouTube Video", payload_yt)

# 2. Test Instagram Video
payload_ig_vid = {"url": "https://www.instagram.com/reel/C8C8Jd2N2qS/", "mode": "ytdlp"}
run_test("Instagram Reel", payload_ig_vid)

# 3. Test Instagram Carousel
payload_ig_car = {"url": "https://www.instagram.com/p/DeMCe6QFMjQ/?img_index=1", "mode": "ytdlp"}
run_test("Instagram Carousel", payload_ig_car)

# Clean up
proc.terminate()
print("Tests completed.")
