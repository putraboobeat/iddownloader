import requests
import time
import subprocess

print("Starting backend...")
proc = subprocess.Popen(["python3", "app.py", "--port", "7777", "--vps"])
time.sleep(2)

API_URL = "http://localhost:7777"
idx = requests.get(API_URL)
token = None
for line in idx.text.splitlines():
    if "const token=" in line:
        token = line.split("const token='")[1].split("'")[0]
        break

headers = {"Content-Type": "application/json", "X-App-Token": token}

urls = "https://www.instagram.com/p/DeQq_-Wy9mn/\nhttps://www.instagram.com/p/DeLalwfMDJ7/"

print("Starting download...")
payload = {
    "url": "https://www.instagram.com/p/DeQq_-Wy9mn/",
    "urls": urls,
    "mode": "ytdlp",
    "name": "bulk test"
}
res = requests.post(f"{API_URL}/start", json=payload, headers=headers)
print("Start result:", res.text)

while True:
    time.sleep(3)
    status_res = requests.get(f"{API_URL}/status", headers=headers)
    status_data = status_res.json()
    if not status_data.get('running'):
        print("Final logs:")
        for log in status_data.get('logs', []):
            print(log)
        break

print("Final files:", status_data.get('files'))
proc.terminate()
