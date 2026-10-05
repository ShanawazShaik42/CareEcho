import io, os, re, threading, json, time, difflib, urllib.request, urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, redirect, render_template_string, make_response, send_file
from dotenv import load_dotenv
from google import genai
from google.genai import types
try:
    from gtts import gTTS
except ImportError:
    gTTS = None

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]  # fallback only; the app asks Google which models exist
DATA_FILE = "data.json"
LANGS = {"hi-IN": "Hindi", "te-IN": "Telugu", "en-IN": "English"}
PIN = os.getenv("SETUP_PIN", "1234")
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")  # optional Telegram caregiver alerts
app = Flask(__name__)


def load():
    try:
        d = json.load(open(DATA_FILE, encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("meds", []); d.setdefault("visit", ""); d.setdefault("diet", "")
    d.setdefault("visited", ""); d.setdefault("log", []); d.setdefault("alerted", [])
    if d.get("lang") not in LANGS:
        d["lang"] = "hi-IN"
    return d


def save(d):
    json.dump(d, open(DATA_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def authed():
    return request.cookies.get("pin") == PIN


def pick_lang(value, d):
    return value if value in LANGS else d["lang"]


KEYS = [k for k in (os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3")) if k]
CLIENTS = [genai.Client(api_key=k) for k in KEYS]
LAST = {"why": "busy"}  # why the last AI call failed: quota or busy
COOLDOWN = {}  # (key number, model) -> time when it may be tried again


MODELS_LIVE = []


def models_to_try():
    """Ask Google which flash models exist today, so retired model names can never break the app."""
    global MODELS_LIVE
    if MODELS_LIVE or not CLIENTS:
        return MODELS_LIVE or MODELS
    found = []
    try:
        for m in CLIENTS[0].models.list():
            n = m.name.replace("models/", "")
            acts = getattr(m, "supported_actions", None) or ["generateContent"]
            bad = ("image", "tts", "live", "audio", "embed", "robotics", "computer", "pro", "gemma", "imagen", "veo", "learnlm")
            if "generateContent" in acts and "flash" in n and not any(b in n for b in bad):
                found.append(n)
    except Exception as e:
        print("Could not list models:", str(e)[:100])
    lite = sorted([n for n in found if "lite" in n], reverse=True)
    full = sorted([n for n in found if "lite" not in n], reverse=True)
    first = [os.getenv("GEMINI_MODEL")] if os.getenv("GEMINI_MODEL") else []
    MODELS_LIVE = list(dict.fromkeys(first + (lite + full)[:6])) or MODELS
    print("AI models in use:", MODELS_LIVE)
    return MODELS_LIVE


def ask(contents, json_out=False):
    cfg = types.GenerateContentConfig(response_mime_type="application/json") if json_out else None
    combos = [(ci, m) for m in models_to_try() for ci in range(len(CLIENTS))]
    for round_ in range(2):
        busy = False
        for ci, model in combos:
            if COOLDOWN.get((ci, model), 0) > time.time():
                continue  # known to be out of quota or broken: skip instantly, no waiting
            try:
                return CLIENTS[ci].models.generate_content(model=model, contents=contents, config=cfg).text
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                print(f"Gemini error (key {ci + 1}, {model}):", msg[:100])
                if "quota" in low or "429" in msg or "exhausted" in low:
                    LAST["why"] = "quota"
                    COOLDOWN[(ci, model)] = time.time() + (3600 if "perday" in low or "per day" in low else 60)
                elif "503" in msg or "unavailable" in low or "demand" in low:
                    LAST["why"] = "busy"
                    COOLDOWN[(ci, model)] = time.time() + 1
                    busy = True
                else:
                    LAST["why"] = "model"
                    COOLDOWN[(ci, model)] = time.time() + 300
        if not busy:
            break
        time.sleep(1.5)
    return None


SPOKEN_CACHE = {}


def spoken(facts, lang):  # only used for the doctor's free-text food advice; result is cached
    k = (facts, lang)
    if k in SPOKEN_CACHE:
        return SPOKEN_CACHE[k], lang
    prompt = (f"Write a spoken message in {LANGS[lang]} for an elderly person who cannot read. "
              "Very simple words, short sentences, plain text, no symbols. Use ONLY the facts below. "
              "Never add or change any advice.\nFACTS: " + facts)
    text = ask(prompt)
    if text:
        SPOKEN_CACHE[k] = text
        return text, lang
    return facts, "en-IN"


DAYPART = {"hi-IN": ["सुबह", "दोपहर", "शाम", "रात"], "te-IN": ["ఉదయం", "మధ్యాహ్నం", "సాయంత్రం", "రాత్రి"],
           "en-IN": ["morning", "afternoon", "evening", "night"]}
WORDS = {"hi-IN": {r"tablets?|pills?": "गोली", r"capsules?": "कैप्सूल", r"after (food|meals?)": "खाने के बाद",
                   r"before (food|meals?)": "खाने से पहले", r"with food": "खाने के साथ",
                   r"at bedtime": "सोने से पहले", r"empty stomach": "खाली पेट"},
         "te-IN": {r"tablets?|pills?": "మాత్ర", r"capsules?": "క్యాప్సూల్", r"after (food|meals?)": "భోజనం తర్వాత",
                   r"before (food|meals?)": "భోజనానికి ముందు", r"with food": "భోజనంతో పాటు",
                   r"at bedtime": "నిద్రపోయే ముందు", r"empty stomach": "ఖాళీ కడుపుతో"},
         "en-IN": {}}


def loc(text, lang):
    for k, v in WORDS[lang].items():
        text = re.sub(k, v, text, flags=re.I)
    return text


def tstr(t, lang):
    h, mi = map(int, t.split(":"))
    dp = DAYPART[lang][0 if 5 <= h < 12 else 1 if 12 <= h < 17 else 2 if 17 <= h < 21 else 3]
    hm = f"{h % 12 or 12}" + (f":{mi:02d}" if mi else "")
    return {"hi-IN": f"{dp} {hm} बजे", "te-IN": f"{dp} {hm} గంటలకు", "en-IN": f"{hm} in the {dp}"}[lang]


def item(m, lang):
    return f'{m["name"]}, {loc(m["dose"], lang)}' + (f', {loc(m["note"], lang)}' if m["note"] else "")


T = {
 "hi-IN": dict(
  now="अब आपकी दवा का समय है। {x}। अभी दवा मत लीजिए। क्या आपने खाना खाया है? हाँ के लिए हरा बटन दबाइए। नहीं के लिए लाल बटन दबाइए।",
  none="अभी कोई दवा लेने का समय नहीं है। अभी दवा मत लीजिए।",
  yes="अच्छा। अब नीला कैमरा बटन दबाइए। सामने रखी सारी गोलियों की फोटो लीजिए।",
  no="पहले कुछ खा लीजिए। खाने के बाद हरा बटन दबाइए।",
  today="आज की दवाएँ। {x}।", per="{n}, {d}। समय {t}",
  nomed="कोई दवा सेव नहीं है। अपने परिवार या स्वास्थ्य कार्यकर्ता से पूछिए।",
  nodiet="खाने की कोई सलाह सेव नहीं है। अपने डॉक्टर से पूछिए।",
  novisit="डॉक्टर के पास जाने की तारीख सेव नहीं है। अपने परिवार से पूछिए।",
  past="डॉक्टर के पास जाने की तारीख निकल चुकी है। परिवार से नई तारीख लेने को कहिए।",
  v0="आज डॉक्टर के पास जाना है। समय {t}।", v1="कल डॉक्टर के पास जाना है। समय {t}।",
  vn="डॉक्टर के पास {n} दिन बाद जाना है। समय {t}।",
  take="अभी ये दवा लीजिए: {x}।", wait="ये दवा अभी मत लीजिए: {x}।",
  unk="ये दवा डॉक्टर की सूची में नहीं है। मत लीजिए: {x}। डॉक्टर या दवा दुकान वाले को दिखाइए।",
  miss="ये दवा लेनी है पर फोटो में नहीं दिखी: {x}। उसे ढूँढकर फिर फोटो लीजिए।",
  unread="फोटो में दवा का नाम पढ़ नहीं पाया। रोशनी में, पट्टी पास रखकर फिर फोटो लीजिए। जाँच होने तक कोई दवा मत लीजिए।",
  taken="बहुत अच्छा। आपकी दवा दर्ज हो गई। आपके परिवार को पता चल जाएगा।"),
 "te-IN": dict(
  now="ఇప్పుడు మీ మందుల సమయం. {x}. ఇంకా మందు వేసుకోకండి. మీరు భోజనం చేశారా? అవును అయితే ఆకుపచ్చ బటన్ నొక్కండి. లేదు అయితే ఎరుపు బటన్ నొక్కండి.",
  none="ఇప్పుడు ఏ మందూ వేసుకునే సమయం కాదు. ఇప్పుడు మందు వేసుకోకండి.",
  yes="మంచిది. ఇప్పుడు నీలం కెమెరా బటన్ నొక్కండి. మీ ముందు ఉన్న మాత్రలన్నిటి ఫోటో తీయండి.",
  no="ముందు కొంచెం తినండి. తిన్న తర్వాత ఆకుపచ్చ బటన్ నొక్కండి.",
  today="ఈ రోజు మందులు. {x}.", per="{n}, {d}. సమయం {t}",
  nomed="ఏ మందులూ సేవ్ చేయలేదు. మీ కుటుంబాన్ని లేదా ఆరోగ్య కార్యకర్తను అడగండి.",
  nodiet="ఆహార సలహా సేవ్ చేయలేదు. మీ డాక్టర్‌ను అడగండి.",
  novisit="డాక్టర్ దగ్గరకు వెళ్లే తేదీ సేవ్ చేయలేదు. మీ కుటుంబాన్ని అడగండి.",
  past="డాక్టర్ దగ్గరకు వెళ్లే తేదీ దాటిపోయింది. కొత్త తేదీ తీసుకోమని మీ కుటుంబానికి చెప్పండి.",
  v0="ఈ రోజు డాక్టర్ దగ్గరకు వెళ్లాలి. సమయం {t}.", v1="రేపు డాక్టర్ దగ్గరకు వెళ్లాలి. సమయం {t}.",
  vn="{n} రోజుల తర్వాత డాక్టర్ దగ్గరకు వెళ్లాలి. సమయం {t}.",
  take="ఇప్పుడు ఈ మందు వేసుకోండి: {x}.", wait="ఈ మందు ఇప్పుడు వేసుకోకండి: {x}.",
  unk="ఈ మందు డాక్టర్ జాబితాలో లేదు. వేసుకోకండి: {x}. డాక్టర్‌కు లేదా మందుల షాపు వారికి చూపించండి.",
  miss="ఈ మందు వేసుకోవాలి కానీ ఫోటోలో కనిపించలేదు: {x}. దాన్ని వెతికి మళ్లీ ఫోటో తీయండి.",
  unread="ఫోటోలో మందు పేరు చదవలేకపోయాను. వెలుగులో, స్ట్రిప్ దగ్గరగా పట్టుకుని మళ్లీ ఫోటో తీయండి. చెక్ అయ్యే వరకు ఏ మందూ వేసుకోకండి.",
  taken="చాలా బాగుంది. మీ మందు నమోదు అయింది. మీ కుటుంబానికి తెలుస్తుంది."),
 "en-IN": dict(
  now="It is time for your tablets. {x}. Do not take them yet. Have you eaten? Press the green button for yes. Press the red button for no.",
  none="No tablet is due right now. Do not take any tablet now.",
  yes="Good. Now press the big blue camera button. Take a photo of all the tablets in front of you.",
  no="Please eat something first. After you eat, press the green button.",
  today="Today's tablets. {x}.", per="{n}, {d}. Time: {t}",
  nomed="No medicines are saved. Ask your family or health worker.",
  nodiet="No food advice is saved. Ask your doctor.",
  novisit="No doctor visit is saved. Ask your family or health worker.",
  past="The saved doctor visit date has passed. Ask your family to book a new visit.",
  v0="You see the doctor today. Time: {t}.", v1="You see the doctor tomorrow. Time: {t}.",
  vn="You see the doctor in {n} days. Time: {t}.",
  take="Take these now: {x}.", wait="Do not take these now: {x}.",
  unk="This is not on the doctor's list. Do not take: {x}. Show it to the doctor or pharmacist.",
  miss="You need this tablet but it is not in the photo: {x}. Find it and take another photo.",
  unread="I could not read the medicine name. Take another photo in good light, holding the strip close. Do not take any tablet until it is checked.",
  taken="Well done. Your tablets are marked as taken. Your family can see it."),
}


def med_line(m):
    return f'{m["name"]}, {m["dose"]}' + (f', {m["note"]}' if m["note"] else "")


def key(m, t):
    return f'{datetime.now():%Y-%m-%d}|{m["name"]}|{t}'


def slot(t):
    h, mi = map(int, t.split(":"))
    return datetime.now().replace(hour=h, minute=mi, second=0, microsecond=0)


def fix_date(v, with_time=False):
    v = (v or "").strip()
    for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(v, f)
        except ValueError:
            continue
        if with_time:
            return dt.strftime("%Y-%m-%dT%H:%M") if "%H" in f else dt.strftime("%Y-%m-%dT09:00")
        return dt.strftime("%Y-%m-%d")
    return ""


def parse_time(t):
    t = t.strip().upper().replace(".", ":").replace(" ", "")
    for f in ("%H:%M", "%I:%M%p", "%I%p", "%H"):
        try:
            return datetime.strptime(t, f).strftime("%H:%M")
        except ValueError:
            pass
    return None


def next_dose(d):
    best = None
    for m in d["meds"]:
        for t in m["times"]:
            s_ = slot(t)
            if key(m, t) in d["log"] or s_ + timedelta(minutes=60) < datetime.now():
                s_ += timedelta(days=1)
            if best is None or s_ < best[0]:
                best = (s_, m["name"])
    return f"{best[1]} {best[0]:%H:%M}" if best else ""


def due_now(d, demo=False):
    if demo:  # demo mode: pretend every medicine is due now
        return [(m, m["times"][0]) for m in d["meds"]]
    now = datetime.now()
    return [(m, t) for m in d["meds"] for t in m["times"]
            if slot(t) - timedelta(minutes=5) <= now <= slot(t) + timedelta(minutes=60) and key(m, t) not in d["log"]]


def missed(d):
    now = datetime.now()
    return [(m, t) for m in d["meds"] for t in m["times"]
            if slot(t) + timedelta(minutes=60) < now <= slot(t) + timedelta(hours=4) and key(m, t) not in d["log"]]


def notify(msg):
    print("CAREGIVER ALERT:", msg)
    if TG_TOKEN and TG_CHAT:
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage?" +
                                   urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}), timeout=8)
        except Exception as e:
            print("Telegram error:", e)


def find_match(name, meds):
    a_name = name.lower().strip()
    if len(a_name) < 3:
        return None
    for m in meds:
        a = m["name"].lower()
        if a in a_name or a_name in a or difflib.SequenceMatcher(None, a, a_name).ratio() > 0.8:
            return m
    return None


def contains(lst, m):
    return any(x is m for x in lst)


CSS = """
:root{--bg:#0e1a3a;--bg2:#22388a;--ink:#eef3ff;--echo:#5ce1e6;--go:#2fbf71;--stop:#ff5d5d;--warn:#ffb020;--card:rgba(255,255,255,.09)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font-family:'Nunito','Noto Sans Devanagari','Noto Sans Telugu',system-ui,sans-serif;color:var(--ink);
background:radial-gradient(circle at 50% -10%,var(--bg2),var(--bg) 70%) fixed;padding:14px 14px 40px}
.wrap{max-width:560px;margin:auto}
h1{margin:6px 0 0;font-size:32px;text-align:center;letter-spacing:.5px}
.sub{opacity:.7;margin:2px 0 8px;text-align:center}
:focus-visible{outline:4px solid var(--warn);outline-offset:3px}
@media(prefers-reduced-motion:reduce){*{animation:none!important}}
"""

HOME = """<!doctype html><html><head><meta charset="utf-8"><title>CareEcho</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@500;800&family=Noto+Sans+Devanagari:wght@500;800&family=Noto+Sans+Telugu:wght@500;800&display=swap" rel="stylesheet">
<style>{{ css|safe }}
body{text-align:center}
#langs{display:flex;gap:8px;margin:10px 0}
#langs button{flex:1;padding:12px 4px;font-size:20px;border-radius:999px;border:2px solid var(--echo);background:transparent;color:var(--echo);font-family:inherit}
#langs button.on{background:var(--echo);color:var(--bg);font-weight:800}
.orb{position:relative;width:160px;height:160px;margin:14px auto}
.orb i{position:absolute;inset:0;border-radius:50%;border:3px solid var(--echo);opacity:0}
.orb b{position:absolute;inset:32px;border-radius:50%;display:grid;place-items:center;font-size:42px;font-weight:400;
background:radial-gradient(circle at 35% 30%,#c9f9fb,var(--echo) 60%,#1aa3b0)}
.orb.speak i{animation:ring 2.2s ease-out infinite}
.orb.speak i:nth-child(2){animation-delay:.7s}.orb.speak i:nth-child(3){animation-delay:1.4s}
@keyframes ring{0%{transform:scale(.55);opacity:.9}100%{transform:scale(1.5);opacity:0}}
.orb.alert b{background:radial-gradient(circle at 35% 30%,#ffe7b3,var(--warn) 65%,#d97a00);animation:throb .8s infinite}
@keyframes throb{50%{transform:scale(1.1)}}
.say{font-size:24px;line-height:1.5;padding:18px;border-radius:24px;background:var(--card);border:1px solid rgba(255,255,255,.18);min-height:84px;margin:6px 0 12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
.tile{padding:20px 6px;font-size:24px;border-radius:24px;border:1px solid rgba(255,255,255,.15);color:var(--ink);background:var(--card);font-family:inherit;font-weight:800}
.tile span{display:block;font-size:44px}
.big{display:block;width:100%;padding:22px;font-size:28px;font-weight:800;border-radius:24px;border:0;margin:12px 0;font-family:inherit;color:#fff;cursor:pointer}
.big em{font-style:normal}
.go{background:var(--go)}.stop{background:var(--stop)}.cam{background:var(--echo);color:var(--bg)}
.row{display:flex;gap:12px}.row .big{margin:12px 0}
.it{font-size:26px;margin:8px 0;padding:14px;border-radius:18px;color:#fff;font-weight:800}
.take{background:var(--go)}.wait{background:var(--stop)}.unk{background:#8b5cf6}.miss{background:#d97706}
.hide{display:none!important}
</style></head><body><div class="wrap">
<h1>CareEcho</h1><p class="sub">Your medicine friend</p><p class="sub" id="next"></p>
<div id="langs">
<button id="l-hi-IN" onclick="setLang('hi-IN')">हिन्दी</button>
<button id="l-te-IN" onclick="setLang('te-IN')">తెలుగు</button>
<button id="l-en-IN" onclick="setLang('en-IN')">English</button>
</div>
<div class="orb" id="orb"><i></i><i></i><i></i><b>🎧</b></div>
<button class="big cam" id="b-start" onclick="start()"></button>
<div class="say" id="out"></div>
<button class="big cam" onclick="repeat()">🔁 <em id="b-repeat"></em></button>
<div id="items"></div>
<div id="eat" class="row hide">
<button class="big go" onclick="ate(1)">🍽️ ✅ <em id="b-yes"></em></button>
<button class="big stop" onclick="ate(0)">🍽️ ❌ <em id="b-no"></em></button>
</div>
<label class="big cam hide" id="photoLabel">📷 <em id="b-photo"></em>
<input type="file" accept="image/*" capture="environment" hidden onchange="photo(this)"></label>
<button class="big go hide" id="took" onclick="took()">✅ <em id="b-took"></em></button>
<div class="grid">
<button class="tile" onclick="checkNow()"><span>💊</span><em id="b-now"></em></button>
<button class="tile" onclick="ask('today')"><span>📅</span><em id="b-today"></em></button>
<button class="tile" onclick="ask('diet')"><span>🍚</span><em id="b-diet"></em></button>
<button class="tile" onclick="ask('visit')"><span>🏥</span><em id="b-visit"></em></button>
</div>
<a class="big cam" href="/schedule" style="text-align:center;text-decoration:none;margin-top:14px">📋 <em id="b-sched"></em></a></div>
<script>
const $=id=>document.getElementById(id);
function shrink(file,max){return new Promise(function(res){const img=new Image();img.onload=function(){const k=Math.min(1,max/Math.max(img.width,img.height));const c=document.createElement('canvas');c.width=img.width*k;c.height=img.height*k;c.getContext('2d').drawImage(img,0,0,c.width,c.height);c.toBlob(function(b){res(b||file);},'image/jpeg',0.85);};img.onerror=function(){res(file);};img.src=URL.createObjectURL(file);});}
const DEF="{{ lang }}";
const DEMO=location.search.indexOf("demo")>=0;
const L={
 'hi-IN':{start:'🔔 शुरू करें',now:'अभी की दवा',today:'आज की दवा',diet:'खाना',visit:'डॉक्टर',photo:'गोलियों की फोटो',yes:'हाँ, खा लिया',no:'नहीं',took:'मैंने दवा ले ली',repeat:'फिर से सुनें',sched:'दवा की सूची'},
 'te-IN':{start:'🔔 ప్రారంభం',now:'ఇప్పుడు మందు',today:'ఈ రోజు మందులు',diet:'ఆహారం',visit:'డాక్టర్',photo:'మాత్రల ఫోటో',yes:'అవును, తిన్నాను',no:'లేదు',took:'మందు వేసుకున్నాను',repeat:'మళ్ళీ వినండి',sched:'మందుల జాబితా'},
 'en-IN':{start:'🔔 START',now:'Now',today:'Today',diet:'Food',visit:'Doctor',photo:'Photo of tablets',yes:'Yes, I ate',no:'No',took:'I took them',repeat:'Hear again',sched:'Schedule'}
};
let LANG=DEF,lastTake=[],last="",started=false,AC=null;
try{const s=localStorage.getItem('lang');if(s&&L[s])LANG=s;}catch(e){}
function setLang(l){
  LANG=l;try{localStorage.setItem('lang',l);}catch(e){}
  for(const k in L){$('l-'+k).className=(k===l?'on':'');}
  for(const k in L[l]){$('b-'+k).textContent=L[l][k];}
}
setLang(LANG);
function orb(on){$('orb').classList.toggle('speak',on);}
let player=null,seq=0,lastText="",lastLang="";
function stopSpeech(){if(player){player.pause();player=null;}speechSynthesis.cancel();orb(0);}
function browserSay(t,lang){
  const u=new SpeechSynthesisUtterance(t);u.lang=lang;u.rate=0.8;
  u.onstart=function(){orb(1);};u.onend=u.onerror=function(){orb(0);};
  speechSynthesis.speak(u);
}
async function say(t,lang){
  lastText=t;lastLang=lang||LANG;$('out').textContent=t;
  stopSpeech();const my=++seq;
  try{
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t,lang:lastLang})});
    if(!r.ok)throw 0;
    const url=URL.createObjectURL(await r.blob());
    if(my!==seq)return;
    player=new Audio(url);
    player.onplay=function(){orb(1);};player.onended=player.onerror=function(){orb(0);};
    await player.play();
  }catch(e){if(my===seq)browserSay(t,lastLang);}
}
function repeat(){if(lastText)say(lastText,lastLang);}
const ICON={take:'✅ ',wait:'⛔ ',unk:'❓ ',miss:'🔍 '};
function show(j){
  const box=$('items');box.innerHTML='';
  (j.items||[]).forEach(function(x){
    const d=document.createElement('div');d.className='it '+x.status;
    d.textContent=ICON[x.status]+x.name;box.appendChild(d);
  });
  say(j.text,j.lang);return j;
}
async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}
async function ask(kind){
  $('out').textContent='…';$('items').innerHTML='';
  return show(await post('/api/say',{kind:kind,lang:LANG,demo:DEMO}));
}
async function checkNow(){
  const j=await ask('now');
  $('eat').classList.toggle('hide',!j.due);
  $('photoLabel').classList.add('hide');$('took').classList.add('hide');
}
async function ate(yes){
  await ask(yes?'ate_yes':'ate_no');
  if(yes){$('eat').classList.add('hide');$('photoLabel').classList.remove('hide');}
}
async function photo(inp){
  if(!inp.files.length)return;
  $('out').textContent='…';
  const f=new FormData();f.append('photo',await shrink(inp.files[0],800),'p.jpg');f.append('lang',LANG);f.append('demo',DEMO?'1':'0');
  const j=show(await (await fetch('/api/photo',{method:'POST',body:f})).json());
  lastTake=j.take||[];
  $('photoLabel').classList.toggle('hide',lastTake.length>0);
  $('took').classList.toggle('hide',!lastTake.length);
  inp.value='';
}
async function took(){
  show(await post('/api/taken',{names:lastTake,lang:LANG,demo:DEMO}));
  $('took').classList.add('hide');$('orb').classList.remove('alert');
}
function beep(n){
  if(!AC)return;let i=0;
  const t=setInterval(function(){
    const o=AC.createOscillator();o.frequency.value=880;o.connect(AC.destination);
    o.start();o.stop(AC.currentTime+.3);if(++i>=n)clearInterval(t);
  },600);
}
function remind(){$('orb').classList.add('alert');beep(3);setTimeout(checkNow,2000);}
fetch('/api/due').then(function(r){return r.json();}).then(function(d){$('next').textContent=d.next?'⏰ '+d.next:'';});
function start(){
  if(started)return;started=true;
  try{AC=new (window.AudioContext||window.webkitAudioContext)();}catch(e){}
  $('b-start').classList.add('hide');
  checkNow();
  let first=true;
  async function poll(){
    const d=await (await fetch('/api/due')).json();
    $('next').textContent=d.next?'⏰ '+d.next:'';
    if(first){first=false;last=d.key;return;}
    if(d.key&&d.key!==last){last=d.key;remind();}
    if(!d.key)last="";
  }
  poll();setInterval(poll,15000);
}
</script></body></html>"""

SETUP = """<!doctype html><html><head><meta charset="utf-8"><title>CareEcho setup</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>{{ css|safe }}
.card{background:var(--card);border:1px solid rgba(255,255,255,.18);border-radius:20px;padding:18px;margin:14px 0}
label{display:block;margin:12px 0 4px;font-weight:800}
input,select,textarea{width:100%;padding:12px;border-radius:12px;border:2px solid rgba(255,255,255,.25);background:rgba(0,0,0,.25);color:var(--ink);font:inherit}
button,.btn{display:inline-block;padding:14px 22px;border-radius:14px;border:0;background:var(--echo);color:var(--bg);font:inherit;font-weight:800;cursor:pointer;margin-top:14px;text-decoration:none}
.warn{border-color:var(--warn);color:#ffe7b3}.ok{color:var(--go);font-weight:800}small{opacity:.7}
</style></head><body><div class="wrap">
<h1>CareEcho setup</h1><p class="sub">For family or health worker</p>
{% if not ok %}
<form method="post" class="card"><label>Enter PIN</label><input type="password" name="pin" autofocus>
<button>Open setup</button> {{ '❌ Wrong PIN' if wrong }}</form>
{% else %}
{% if missed_list %}<div class="card warn"><b>⚠️ Missed doses (last 4 hours)</b><br>{% for x in missed_list %}{{ x }}<br>{% endfor %}</div>{% endif %}
{% if skipped %}<div class="card warn"><b>⚠️ Not saved (time missing or wrong; use 08:00,20:00):</b> {{ skipped }}</div>{% endif %}
{% if d.meds %}<div class="card"><b>✅ Saved reminders (check carefully)</b><br>{% for m in d.meds %}{{ m.name }} — {{ m.dose }} — {{ m.times|join(', ') }}<br>{% endfor %}</div>{% endif %}
<div class="card"><b>📄 Fill from discharge paper</b><br><small>Take a clear photo. AI reads it. Check everything before saving.</small><br>
<label class="btn">Choose photo<input type="file" accept="image/*" capture="environment" hidden onchange="paper(this)"></label>
<p id="st"></p></div>
<form method="post" class="card">
<label>Default language</label>
<select name="lang">{% for k,v in langs.items() %}<option value="{{k}}" {{ 'selected' if k==d.lang }}>{{v}}</option>{% endfor %}</select>
<label>Medicines, one per line: name | dose | times | note</label>
<textarea id="meds" name="meds" rows="6" placeholder="Paracetamol 500mg | 1 tablet | 08:00,20:00 | after food">{{ meds_text }}</textarea>
<label>Next doctor visit (revisit date from slip)</label><input id="visit" type="datetime-local" name="visit" value="{{ d.visit }}">
<label>Date of visit / discharge (from slip)</label><input id="visited" type="date" name="visited" value="{{ d.visited }}">
<label>Food advice from doctor</label><textarea id="diet" name="diet" rows="3">{{ d.diet }}</textarea>
<button>Save</button> <span class="ok">{{ '✅ Saved' if saved }}</span>
</form>
<a class="btn" href="/">Open elder screen</a> <a class="btn" href="/schedule">View schedule</a>
<script>
const $=id=>document.getElementById(id);
function shrink(file,max){return new Promise(function(res){const img=new Image();img.onload=function(){const k=Math.min(1,max/Math.max(img.width,img.height));const c=document.createElement('canvas');c.width=img.width*k;c.height=img.height*k;c.getContext('2d').drawImage(img,0,0,c.width,c.height);c.toBlob(function(b){res(b||file);},'image/jpeg',0.85);};img.onerror=function(){res(file);};img.src=URL.createObjectURL(file);});}
async function paper(inp){
  if(!inp.files.length)return;
  $('st').textContent='Reading the paper…';
  const f=new FormData();f.append('photo',await shrink(inp.files[0],1600),'p.jpg');
  const j=await (await fetch('/api/extract',{method:'POST',body:f})).json();
  if(j.error){$('st').textContent=j.error;return;}
  $('meds').value=(j.meds||[]).map(function(m){return [m.name,m.dose||'',(m.times||[]).join(','),m.note||''].join(' | ');}).join('\\n');
  if(j.visit)$('visit').value=j.visit;
  if(j.visited)$('visited').value=j.visited;
  if(j.diet)$('diet').value=j.diet;
  $('st').textContent='✅ Read. Saving the reminders…';document.querySelector('form').submit();
}
</script>
{% endif %}
</div></body></html>"""


SCHEDULE = """<!doctype html><html><head><meta charset="utf-8"><title>Tablet schedule</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>{{ css|safe }}
.dates{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:14px 0}
.dc{background:var(--card);border:1px solid rgba(255,255,255,.18);border-radius:20px;padding:16px}
.dc small{opacity:.8;display:block}.dc b{display:block;font-size:20px;margin-top:6px}.dc i{font-style:normal;color:var(--echo);font-weight:800}
.med{background:var(--card);border-left:8px solid var(--echo);border-radius:18px;padding:14px;margin:12px 0}
.med h3{margin:0 0 4px;font-size:24px}
.chip{display:inline-block;margin:8px 8px 0 0;padding:8px 14px;border-radius:999px;background:rgba(92,225,230,.18);font-weight:800}
.chip.done{background:var(--go);color:#fff}
a.btn{display:inline-block;padding:14px 20px;border-radius:14px;background:var(--echo);color:var(--bg);font-weight:800;text-decoration:none;margin:8px 8px 0 0}
</style></head><body><div class="wrap">
<h1>Tablet schedule</h1><p class="sub">Everything the reminders use</p>
<div class="dates">
<div class="dc"><small>🏥 Visited / discharged on</small><b>{{ visited }}</b></div>
<div class="dc"><small>📅 Next doctor visit</small><b>{{ revisit }}</b><i>{{ left }}</i></div>
</div>
{% for m in meds %}<div class="med"><h3>💊 {{ m.name }}</h3>{{ m.dose }}{% if m.note %} · {{ m.note }}{% endif %}<br>
{% for c in m.chips %}<span class="chip {{ 'done' if c.done }}">{{ '✅ ' if c.done }}{{ c.label }}</span>{% endfor %}</div>
{% else %}<div class="dc">No medicines saved yet. Use the setup page.</div>{% endfor %}
<a class="btn" href="/">Elder screen</a><a class="btn" href="/?demo=1">▶ Demo reminder</a><a class="btn" href="/setup">Setup</a>
</div></body></html>"""


@app.route("/setup", methods=["GET", "POST"])
def setup():
    wrong = False
    if request.method == "POST" and "pin" in request.form:
        if request.form["pin"] == PIN:
            resp = make_response(redirect("/setup"))
            resp.set_cookie("pin", PIN, max_age=86400)
            return resp
        wrong = True
    if not authed():
        return render_template_string(SETUP, css=CSS, ok=False, wrong=wrong)
    d = load()
    if request.method == "POST":
        meds, skipped = [], []
        for line in request.form["meds"].splitlines():
            p = [x.strip() for x in line.split("|")]
            if not p[0]:
                continue
            if len(p) < 3:
                skipped.append(p[0])
                continue
            times = [x for x in (parse_time(t) for t in p[2].split(',')) if x]
            if not times:
                skipped.append(p[0])
            if times:
                meds.append({"name": p[0], "dose": p[1], "times": times, "note": p[3] if len(p) > 3 else ""})
        d.update(lang=request.form["lang"] if request.form["lang"] in LANGS else "hi-IN",
                 meds=meds, visit=request.form["visit"], visited=request.form.get("visited", ""), diet=request.form["diet"].strip())
        save(d)
        threading.Thread(target=prewarm_dynamic, args=(d,), daemon=True).start()
        return redirect("/setup?saved=1" + ("&skipped=" + urllib.parse.quote(", ".join(skipped)) if skipped else ""))
    text = "\n".join(f'{m["name"]} | {m["dose"]} | {",".join(m["times"])} | {m["note"]}' for m in d["meds"])
    ml = [f'{m["name"]} at {t}' for m, t in missed(d)]
    return render_template_string(SETUP, css=CSS, ok=True, d=d, langs=LANGS, meds_text=text,
                                  saved=request.args.get("saved"), missed_list=ml, skipped=request.args.get("skipped"))


@app.route("/")
def home():
    return render_template_string(HOME, css=CSS, lang=load()["lang"])


@app.route("/keytest")
def keytest():
    out = [f"Keys found in .env: {len(CLIENTS)}", ""]
    for ci, c in enumerate(CLIENTS):
        for model in models_to_try():
            try:
                c.models.generate_content(model=model, contents="Say OK")
                out.append(f"key {ci + 1} | {model} | WORKS")
            except Exception as e:
                out.append(f"key {ci + 1} | {model} | FAILED: {str(e)[:90]}")
    return "\n".join(out), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/schedule")
def schedule():
    d = load()
    vd = fix_date(d.get("visited"))
    visited = datetime.strptime(vd, "%Y-%m-%d").strftime("%d %B %Y") if vd else "Not on the slip"
    try:
        v = datetime.fromisoformat(d["visit"])
        days = (v.date() - datetime.now().date()).days
        revisit = f"{v:%d %B %Y}, " + v.strftime("%I:%M %p").lstrip("0")
        left = "Today" if days == 0 else "Tomorrow" if days == 1 else f"In {days} days" if days > 1 else "Date passed"
    except Exception:
        revisit, left = "Not on the slip", ""
    meds = [dict(m, chips=[{"label": datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0"),
                            "done": key(m, t) in d["log"]} for t in m["times"]]) for m in d["meds"]]
    return render_template_string(SCHEDULE, css=CSS, visited=visited, revisit=revisit, left=left, meds=meds)


@app.route("/api/due")
def api_due():
    d = load()
    new = [(m, t) for m, t in missed(d) if key(m, t) not in d["alerted"]]
    for m, t in new:
        notify(f'⚠️ CareEcho: missed dose - {m["name"]} at {t}')
        d["alerted"].append(key(m, t))
    if new:
        save(d)
    return jsonify({"key": "|".join(f'{m["name"]}@{t}' for m, t in due_now(d)), "next": next_dose(d)})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    if not authed():
        return jsonify(error="PIN needed"), 403
    f = request.files["photo"]
    part = types.Part.from_bytes(data=f.read(), mime_type=f.mimetype or "image/jpeg")
    raw = ask([part, 'This is a hospital discharge paper. Extract the medicines, the next doctor visit and food advice. '
                     'Reply JSON only: {"meds":[{"name":"","dose":"","times":["HH:MM"],"note":""}],'
                     '"visit":"next doctor revisit date as YYYY-MM-DDTHH:MM, or empty","visited":"date the patient visited or was discharged as YYYY-MM-DD, or empty","diet":""}. '
                     'Dates on Indian slips are day first (DD/MM/YYYY). Use 24-hour times: morning=08:00, '
                     'afternoon=14:00, evening or night=20:00, twice a day=08:00,20:00, three times a day=08:00,14:00,20:00. '
                     'Copy medicine names exactly as printed. Never guess: leave a field empty if it is not written.'],
              json_out=True)
    if raw is None:
        return jsonify(error=("The free AI limit is used up for now. Type the medicines in the boxes below, or add a second API key to .env."
                              if LAST["why"] == "quota" else "No working AI model was found. Open /keytest to check."
                              if LAST["why"] == "model" else "The AI is busy. Please try again in a minute."))
    try:
        j = json.loads(raw)
        j["meds"] = [m for m in j.get("meds", []) if isinstance(m, dict) and m.get("name")]
        j["visit"] = fix_date(j.get("visit"), True)
        j["visited"] = fix_date(j.get("visited"))
        return jsonify(j)
    except Exception:
        return jsonify(error="Could not read the paper. Please try a clearer photo.")


T["hi-IN"]["busy"] = "जाँच अभी व्यस्त है। एक मिनट रुककर फिर कैमरा बटन दबाइए। जाँच होने तक कोई दवा मत लीजिए।"
T["te-IN"]["busy"] = "చెకింగ్ ఇప్పుడు బిజీగా ఉంది. ఒక నిమిషం ఆగి మళ్లీ కెమెరా బటన్ నొక్కండి. చెక్ అయ్యే వరకు ఏ మందూ వేసుకోకండి."
T["en-IN"]["busy"] = "The check is busy right now. Wait one minute, then press the camera button again. Do not take any tablet until it is checked."


@app.route("/api/say", methods=["POST"])
def api_say():
    d = load()
    body = request.json or {}
    kind, lang = body.get("kind"), pick_lang(body.get("lang"), d)
    t, due = T[lang], []
    if kind == "now":
        due = due_now(d, body.get("demo"))
        r = t["now"].format(x=". ".join(f"{item(m, lang)}, {tstr(tm, lang)}" for m, tm in due)) if due else t["none"]
    elif kind == "ate_yes":
        r = t["yes"]
    elif kind == "ate_no":
        r = t["no"]
    elif kind == "today":
        r = t["today"].format(x=". ".join(t["per"].format(n=m["name"], d=loc(m["dose"], lang),
                                                          t=", ".join(tstr(x, lang) for x in m["times"]))
                                          for m in d["meds"])) if d["meds"] else t["nomed"]
    elif kind == "diet":
        if d["diet"]:
            text, used = spoken("Food advice from the doctor: " + d["diet"], lang)
            return jsonify({"text": text, "lang": used, "items": [], "due": False})
        r = t["nodiet"]
    else:
        try:
            v = datetime.fromisoformat(d["visit"])
            days, tm = (v.date() - datetime.now().date()).days, tstr(f"{v:%H:%M}", lang)
            r = t["past"] if days < 0 else t["v0"].format(t=tm) if days == 0 else \
                t["v1"].format(t=tm) if days == 1 else t["vn"].format(n=days, t=tm)
        except Exception:
            r = t["novisit"]
    return jsonify({"text": r, "lang": lang, "items": [], "due": bool(due)})


TTS_CACHE = {}


def tts_bytes(text, lang):
    k = (text, lang)
    if k not in TTS_CACHE:
        buf = io.BytesIO()
        gTTS(text=text[:900], lang=lang.split("-")[0], tld="co.in", slow=True).write_to_fp(buf)
        TTS_CACHE[k] = buf.getvalue()
    return TTS_CACHE[k]


def prewarm():
    if gTTS is None:
        return
    for l in LANGS:
        for k in ("none", "yes", "no", "unread", "taken", "nomed"):
            try:
                tts_bytes(T[l][k], l)
            except Exception as e:
                print("prewarm:", e)
                return


def prewarm_dynamic(d):
    if gTTS is None or not d["meds"]:
        return
    for l in LANGS:
        try:
            combos = [". ".join(f"{item(m, l)}, {tstr(t, l)}" for m in d["meds"] if t in m["times"])
                      for t in sorted({t for m in d["meds"] for t in m["times"]})]
            combos.append(". ".join(f"{item(m, l)}, {tstr(m['times'][0], l)}" for m in d["meds"]))  # demo mode
            for x in combos:
                tts_bytes(T[l]["now"].format(x=x), l)
        except Exception as e:
            print("prewarm:", e)
            return


@app.route("/api/tts", methods=["POST"])
def api_tts():
    b = request.json or {}
    lang = b.get("lang")
    if gTTS is None or lang not in LANGS or not b.get("text"):
        return "", 503
    try:
        return send_file(io.BytesIO(tts_bytes(b["text"], lang)), mimetype="audio/mpeg")
    except Exception as e:
        print("TTS error:", e)
        return "", 503


@app.route("/api/taken", methods=["POST"])
def api_taken():
    d = load()
    b = request.json or {}
    lang = pick_lang(b.get("lang"), d)
    if not b.get("demo"):
        for m, t in due_now(d):
            if m["name"] in b.get("names", []):
                d["log"].append(key(m, t))
        save(d)
    return jsonify({"text": T[lang]["taken"], "lang": lang, "items": []})


@app.route("/api/photo", methods=["POST"])
def api_photo():
    d = load()
    lang = pick_lang(request.form.get("lang"), d)
    f = request.files["photo"]
    part = types.Part.from_bytes(data=f.read(), mime_type=f.mimetype or "image/jpeg")
    raw = ask([part, 'Read the names of ALL medicines PRINTED on the strips, boxes or bottles in this photo. '
                     'Do not guess from tablet shape or colour. Reply as JSON only: {"names": ["..."]}. '
                     'Use an empty list if no name is clearly readable.'], json_out=True)
    if raw is None:
        return jsonify({"text": T[lang]["busy"], "lang": lang, "items": [], "take": []})
    try:
        names = [n for n in json.loads(raw).get("names", []) if isinstance(n, str) and n.strip()]
    except Exception:
        names = []

    due_meds = []
    demo = request.form.get("demo") == "1"
    for m, t in due_now(d, demo):
        if not contains(due_meds, m):
            due_meds.append(m)

    seen, unknown = [], []
    for n in names:
        m = find_match(n, d["meds"])
        if m is None:
            if n not in unknown:
                unknown.append(n)
        elif not contains(seen, m):
            seen.append(m)

    take = [m for m in seen if contains(due_meds, m)]
    wait = [m for m in seen if not contains(due_meds, m)]
    missing = [m for m in due_meds if not contains(seen, m)]

    items, parts, t = [], [], T[lang]
    if not names:
        r = t["unread"]
    else:
        if take:
            parts.append(t["take"].format(x=". ".join(item(m, lang) for m in take)))
            items += [{"name": m["name"], "status": "take"} for m in take]
        if wait:
            parts.append(t["wait"].format(x=", ".join(m["name"] for m in wait)))
            items += [{"name": m["name"], "status": "wait"} for m in wait]
        if unknown:
            parts.append(t["unk"].format(x=", ".join(unknown)))
            items += [{"name": n, "status": "unk"} for n in unknown]
        if missing:
            parts.append(t["miss"].format(x=", ".join(m["name"] for m in missing)))
            items += [{"name": m["name"], "status": "miss"} for m in missing]
        r = " ".join(parts)
    return jsonify({"text": r, "lang": lang, "items": items, "take": [m["name"] for m in take]})

threading.Thread(target=lambda: (models_to_try(), prewarm(), prewarm_dynamic(load())), daemon=True).start()

import difflib, json, re, os
from flask import request, jsonify

def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()

def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0
    if a in b or b in a:
        return 100
    return int(difflib.SequenceMatcher(None, a, b).ratio() * 100)

def _ask_gemini_about_pill(image_bytes, mime, names):
    from google.genai import types
    prompt = (
        "You are checking a medicine strip or tablet photo for an elderly patient.\n"
        "Prescribed medicines: " + ", ".join(names) + "\n"
        "Read any visible text (brand name, generic name, strength). Use colour, shape "
        "and packaging too. Pick the prescribed medicine this most likely is, or 'none'.\n"
        'Reply ONLY with JSON: {"visible_text": "...", "best_match": "<name from list or none>", '
        '"confidence": "high|medium|low"}'
    )
    text = ask([types.Part.from_bytes(data=image_bytes, mime_type=mime), prompt], json_out=True)
    text = re.sub(r"```json|```", "", text or "").strip()
    return json.loads(text)

@app.route("/verify_pill", methods=["POST"])
def verify_pill():
    d = load()
    meds = d.get("meds", [])
    names = [m["name"] for m in meds]
    f = request.files.get("photo")
    if not f or not names:
        return jsonify(status="retry", message="Please show the tablet again.")
    try:
        g = _ask_gemini_about_pill(f.read(), f.mimetype or "image/jpeg", names)
    except Exception as e:
        import traceback; traceback.print_exc()
        print("VERIFY ERROR:", repr(e))
        return jsonify(status="unsure", message="I am not sure. Please wait, I will call your family.")

    best, conf = g.get("best_match", "none"), g.get("confidence", "low")
    seen = g.get("visible_text", "")
    print("PILL DEBUG -> saved:", names, "| read:", seen, "| gemini pick:", best, conf)

    # Score = best of: Gemini's own pick, and fuzzy match of the text it read
    scores = {n: _similar(seen, n) for n in names}
    if best in names:
        scores[best] = max(scores[best], 90 if conf == "high" else 75 if conf == "medium" else 55)
    top = max(scores, key=scores.get)
    score = scores[top]

    if score >= 80:
        return jsonify(status="ok", medicine=top, score=score)
    if score >= 60:
        return jsonify(status="retry", message="Please hold the strip closer and flat.", score=score)
    return jsonify(status="unsure", message="I am not sure. Please do not take it yet. I will alert your family.", score=score)

if __name__ == "__main__":
    app.run(debug=True)