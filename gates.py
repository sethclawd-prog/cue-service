#!/usr/bin/env python3
"""Fetch a mix through a free-download gate (Hypeddit, ToneDen, an artist's own page) using the user's OWN logged-in Chrome
on this Mac, via browser-harness. The gate's steps are performed as the person would: social buttons are clicked (they open
the artist's page in a tab, which is closed again), an email step gets the configured address, and the page's Download
control is pressed. Nothing is bypassed; if a step needs an account this Chrome is not signed into, the run reports it.

Usage:  gates.py <url> [email]       -> prints JSON {"file": path} or {"error": ...}
It shells out to `browser-harness` (must be on PATH, Chrome running with remote debugging), so it can be called from the
service without importing the harness.
"""
import glob, json, os, subprocess, sys, time

INBOX = os.path.expanduser("~/Mixtapes/inbox")
SCRIPT = r'''
import builtins, glob, json, os, re, time
INBOX = __INBOX__; EMAIL = __EMAIL__; URL = __URL__
os.makedirs(INBOX, exist_ok=True)
before = set(glob.glob(INBOX + "/*"))
log = []
# run.py exec()s this text inside a function, so names defined here are locals; helper functions below need them as globals.
builtins.__dict__.update(glob=glob, json=json, os=os, re=re, time=time, INBOX=INBOX, EMAIL=EMAIL, before=before, log=log)
def note(m): log.append(m)
def new_files(done_only=True):
    fs = [f for f in glob.glob(INBOX + "/*") if f not in before]
    return [f for f in fs if not f.endswith(".crdownload")] if done_only else fs
VIS = r"""const vis=e=>{if(!e.offsetParent&&getComputedStyle(e).position!=='fixed')return false;const s=getComputedStyle(e);if(s.visibility==='hidden'||s.opacity==='0')return false;const r=e.getBoundingClientRect();return r.width>2&&r.height>2&&r.bottom>0&&r.right>0&&r.top<innerHeight+2000;};"""
builtins.VIS = VIS
def controls():
    return json.loads(js(r"""(()=>{%s return JSON.stringify([...document.querySelectorAll('button, a, input[type=submit], [role=button]')]
      .map((e,i)=>({e,i})).filter(({e})=>vis(e) && !e.disabled)
      .map(({e,i})=>({i, t:e.tagName, txt:(e.innerText||e.value||'').trim().replace(/\s+/g,' ').slice(0,80), id:e.id, cls:(e.className||'').toString().slice(0,80), href:e.getAttribute('href')||''}))
      .filter(e=>e.txt||e.id))})()""" % VIS) or "[]")
def inputs():
    return json.loads(js(r"""(()=>{%s return JSON.stringify([...document.querySelectorAll('input')].filter(e=>vis(e)&&!e.disabled&&['text','email',''].includes(e.type)).map(e=>({id:e.id,name:e.name,type:e.type,ph:e.placeholder||'',val:e.value||'',label:(e.labels&&e.labels[0]&&e.labels[0].innerText)||''})))})()""" % VIS) or "[]")
def click_ctrl(c):
    sel = ("#" + c["id"]) if c["id"] and re.match(r"^[A-Za-z][\w-]*$", c["id"]) else None
    if sel: js("document.querySelector(%s).click()" % json.dumps(sel))
    else: js(r"""(()=>{const es=[...document.querySelectorAll('button, a, input[type=submit], [role=button]')]; const e=es[%d]; if(e) e.click();})()""" % c["i"])
def close_spawned(keep):
    """Close tabs the gate opened. An OAuth consent page gets its Agree/Allow pressed; a sign-in page means this Chrome
    is not logged into that service, which is reported and ends the run (we never enter credentials)."""
    needs = None
    for t in list_tabs(include_chrome=False):
        if t["targetId"] != keep and t["targetId"] not in start_tabs:
            u = t["url"]
            if re.search(r"accounts\.spotify\.com|soundcloud\.com/connect|accounts\.google\.com|facebook\.com/(login|dialog)|/login|/signin", u):
                switch_tab(t["targetId"]); wait_for_load(); wait(1.5)
                if re.search(r"/login|/signin|login\?", u) or inputs():
                    needs = re.sub(r"^www\.|^accounts\.", "", re.match(r"https?://([^/]+)", u).group(1))
                    note("needs a sign-in this Chrome doesn't have: " + needs)
                else:
                    for c in controls():
                        if re.search(r"^(agree|authorize|allow|connect|continue|accept)\b", c["txt"], re.I):
                            click_ctrl(c); note("auth tab: clicked " + c["txt"]); wait(3); break
            try: cdp("Target.closeTarget", targetId=t["targetId"])
            except Exception: pass
    switch_tab(keep)
    return needs

cdp("Browser.setDownloadBehavior", behavior="allow", downloadPath=INBOX, eventsEnabled=True)
start_tabs = {t["targetId"] for t in list_tabs(include_chrome=False)}; builtins.start_tabs = start_tabs
tid = new_tab(URL); wait_for_load(); wait(2)
SKIP = re.compile(r"privacy|terms|dmca|help|top 100|cookie|policy|sign ?up|log ?in|create account", re.I)
DOWNLOAD = re.compile(r"^(download( now| full mix| mp3| track| the mix)?|get (the |your |my )?(file|track|download|mix|set|it)|unlock(ed)?( download| the mix)?|free download|claim( your)?( download| mix)?)\b", re.I)
COOKIE = re.compile(r"^(accept( all| cookies)?|i agree|got it|allow all|ok)$", re.I)
SOCIAL = re.compile(r"\b(follow|like|repost|subscribe|save|add to (my )?playlist|pre-?save)\b", re.I)
NEXT = re.compile(r"^(next|skip|continue|proceed|i('| a)?m done|done)\b", re.I)
SUBMIT = re.compile(r"(share email|submit|continue|next|unlock|get|send|join)", re.I)
seen_social = set(); tried_dl = 0; email_tries = 0; cookie_done = False; blocked = None
for step in range(14):
    if new_files(done_only=False): note("download started"); break
    if blocked: break
    cs = [c for c in controls() if not SKIP.search(c["txt"])]
    ck = [c for c in cs if COOKIE.match(c["txt"])]
    if ck and not cookie_done: cookie_done = True; click_ctrl(ck[0]); note("clicked cookie " + ck[0]["txt"]); wait(1)
    cs = [c for c in cs if not COOKIE.match(c["txt"])]
    ins = inputs()
    # A Download control that did nothing twice is locked behind the other steps: stop preferring it.
    dl = [c for c in cs if (DOWNLOAD.search(c["txt"]) or c["id"] == "gateDownloadButton")] if tried_dl < 2 else []
    if dl: tried_dl += 1
    email = [i for i in ins if "mail" in (i["id"] + i["name"] + i["type"] + i["ph"] + i["label"]).lower()]
    if email and email_tries < 2:
        i = email[0]; sel = ("#" + i["id"]) if i["id"] else ("input[name=%s]" % json.dumps(i["name"]))
        if i["val"] != EMAIL:
            js("(()=>{const e=document.querySelector(%s); e.focus(); e.value='';})()" % json.dumps(sel)); type_text(EMAIL)
            js("(()=>{const e=document.querySelector(%s); e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));})()" % json.dumps(sel))
            note("filled email"); wait(0.5)
        sub = [c for c in cs if SUBMIT.search(c["txt"]) and not SOCIAL.search(c["txt"]) and not DOWNLOAD.search(c["txt"]) and not NEXT.search(c["txt"])] or [c for c in cs if NEXT.search(c["txt"])]
        email_tries += 1
        if sub: click_ctrl(sub[0]); note("clicked " + sub[0]["txt"]); wait(3); continue
    if len(log) >= 3 and log[-1] == log[-2] == log[-3]: note("stuck on " + log[-1]); break
    if dl:
        click_ctrl(dl[0]); note("clicked " + dl[0]["txt"]); wait(4)
        if new_files(done_only=False): note("download started"); break
        blocked = close_spawned(tid); continue
    soc = [c for c in cs if SOCIAL.search(c["txt"]) and c["txt"] not in seen_social]
    if soc:
        c = soc[0]; seen_social.add(c["txt"]); click_ctrl(c); note("clicked " + c["txt"]); wait(3); blocked = close_spawned(tid)
        if blocked: break
        nx = [c for c in controls() if NEXT.search(c["txt"])]
        if nx: click_ctrl(nx[0]); note("clicked " + nx[0]["txt"]); wait(2)
        continue
    nx = [c for c in cs if NEXT.search(c["txt"])]
    if nx: click_ctrl(nx[0]); note("clicked " + nx[0]["txt"]); wait(2); continue
    note("no known control; visible: " + "; ".join(c["txt"][:30] for c in cs[:12])); break
t_start = time.time() + 15          # a click may take a moment to turn into a download
while time.time() < t_start and not new_files(done_only=False):
    time.sleep(1)
deadline = time.time() + (300 if new_files(done_only=False) else 0)
while time.time() < deadline and not new_files():
    time.sleep(2)
done = new_files()
shot = screenshot("/tmp/gate-last.png")
try: cdp("Target.closeTarget", targetId=tid)
except Exception: pass
print("@@RESULT@@" + json.dumps({"file": done[0] if done else None, "needs_login": blocked, "log": log, "screenshot": shot, "url": URL}))
'''

def fetch(url, email):
    script = SCRIPT.replace("__INBOX__", repr(INBOX)).replace("__EMAIL__", repr(email)).replace("__URL__", repr(url))
    p = subprocess.run(["browser-harness"], input=script, capture_output=True, text=True, timeout=420)
    out = p.stdout + p.stderr
    if "@@RESULT@@" not in out:
        return {"error": "harness failed", "detail": out[-800:]}
    r = json.loads(out.split("@@RESULT@@", 1)[1].strip().splitlines()[0])
    if not r.get("file"):
        r["error"] = "no download started; " + (r["log"][-1] if r.get("log") else "")
    return r

if __name__ == "__main__":
    print(json.dumps(fetch(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CUE_GATE_EMAIL", "")), indent=1))
