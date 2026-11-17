#!/usr/bin/env python3
"""Headless screenshots of the studio (dev check). python -I ui-test.py <base url> <job id>   (needs playwright + a chromium)"""
import glob, os, sys, time
from playwright.sync_api import sync_playwright

BASE, JID = sys.argv[1], sys.argv[2]
OUT = os.path.dirname(os.path.abspath(__file__)) + "/shots"
os.makedirs(OUT, exist_ok=True)
SHELL = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))[0]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=SHELL, args=["--autoplay-policy=no-user-gesture-required"])
    logs = []
    for name, w, h in (("desk", 1280, 900), ("phone", 390, 844)):
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.on("pageerror", lambda e: logs.append(f"PAGEERROR {e}"))
        pg.on("console", lambda m: logs.append(f"{m.type}: {m.text[:160]}") if m.type in ("error", "warning") else None)
        pg.goto(f"{BASE}/#make"); pg.fill("#q", "Linkin Park - Numb"); pg.click("#sbtn")
        pg.wait_for_selector(".makebar", timeout=30000); time.sleep(1.5)
        pg.screenshot(path=f"{OUT}/{name}-search.png", full_page=False)
        pg.goto(f"{BASE}/#jobs/{JID}"); pg.wait_for_selector("#pv", timeout=15000)
        pg.evaluate("document.querySelector('#pv').muted = true")
        pg.evaluate("v => { v.currentTime = 29; v.play(); }", pg.query_selector("#pv"))
        time.sleep(4)
        pg.screenshot(path=f"{OUT}/{name}-preview.png", full_page=True)
        pg.goto(f"{BASE}/#lib"); pg.wait_for_selector("table", timeout=15000); time.sleep(0.5)
        pg.screenshot(path=f"{OUT}/{name}-library.png", full_page=True)
        pg.close()
    b.close()
print("\n".join(dict.fromkeys(logs)) or "no page errors")
