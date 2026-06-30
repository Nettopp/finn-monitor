import os
import json
import re
import smtplib
import time
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

from bs4 import BeautifulSoup
import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEEN_FILE = "seen_ids_trolley.json"
MARKET_FILE = "market_data_trolley.json"
SEEN_DAYS = 30
MARKET_MAX_ITEMS = 500
EVAL_BATCH_SIZE = 25
SCORE_THRESHOLD = 50   # alle treff som matcher beskrivelsen
SCORE_KUPP = 80        # klart underpriset

FINN_SEARCH_URL = "https://www.finn.no/recommerce/forsale/search"
SEARCH_QUERIES = [
    "transportvogn",
    "materialtralle",
    "byggetralle",
    "transporttralle",
    "lagervogn",
    "plattformvogn",
    "godsvogn",
    "industrivogn",
]
CATEGORY_URLS = []

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BUYER_PROFILE = """
Du evaluerer brukte transportvogner og materialtraller til salgs på Finn.no.

REFERANSEPRODUKT:
Transportvogn type AJ Produkter 17805/17807 — robust industri-/håndverksvogn for tunge laster.
Ny pris: ca. 15 900 kr inkl. mva. To- og firehjulsstyring er begge OK.
Se: https://www.ajprodukter.no/p/transportvogn-17805-17807

TEKNISKE KRAV (MÅ oppfylles for score over 40):
- Lengde: 150–200 cm
- Bredde: 75–100 cm
- To- eller firehjulsstyring (begge OK)
- IKKE gipsvogn med hev/senk-mekanisme
- IKKE billige lett-traller fra Clas Ohlson, Jula, IKEA, Biltema o.l. (disse er for små og lette)
- Tunge industri-/lagertraller av stål er ideelt

Hvis størrelse ikke er oppgitt: gi skjønnsmessig score basert på bilde og beskrivelse vs. referanseproduktet.

PRISVURDERING:
- Under 3 000 kr: score 80–100 (kupp)
- 3 000–6 000 kr: score 65–79 (interessant)
- 6 000–10 000 kr: score 50–64 (akseptabelt)
- Over 10 000 kr: score under 50

LOKASJON:
Kjøper kan hente innenfor ca. 1,5 times kjøring fra Oslo sentrum.
Dette dekker: Østfold, Vestfold (Tønsberg/Sandefjord), Kongsberg, Hamar, Gjøvik, Hønefoss.
Utenfor rekkevidde (eksempler): Bergen, Stavanger, Trondheim, Kristiansand, Tromsø, Bodø — gi score 0.
Unntak: hvis teksten nevner "kan sendes", "frakt", "sender" eller "levering", er lokasjon irrelevant.
"""

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen: dict):
    cutoff = (datetime.now() - timedelta(days=SEEN_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items() if v.get("date", "") >= cutoff}
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def load_market_data() -> list:
    if os.path.exists(MARKET_FILE):
        with open(MARKET_FILE) as f:
            return json.load(f)
    return []


def save_market_data(data: list):
    if len(data) > MARKET_MAX_ITEMS:
        data = sorted(data, key=lambda x: x.get("date", ""), reverse=True)[:MARKET_MAX_ITEMS]
    with open(MARKET_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def build_market_summary(market_data: list) -> str:
    if not market_data:
        return ""
    prices = [i["price_kr"] for i in market_data if i.get("price_kr", 0) > 0]
    if not prices:
        return ""
    avg = int(sum(prices) / len(prices))
    low = min(prices)
    best = sorted(market_data, key=lambda x: -x.get("score", 0))[:5]
    lines = [
        f"Markedsdata fra {len(market_data)} tidligere observerte Finn.no-annonser "
        f"(snitt {avg:,} kr, laveste {low:,} kr):"
    ]
    for b in best:
        lines.append(f"  · {b['title']} — {b['price_kr']:,} kr (score {b['score']})")
    return "\n".join(lines)


def append_to_market_data(market_data: list, listings: list) -> list:
    existing_ids = {item.get("id") for item in market_data if item.get("id")}
    now = datetime.now().isoformat()
    for l in listings:
        finn_id = l.get("id", "")
        if finn_id and finn_id in existing_ids:
            continue
        if not l.get("price_kr"):
            continue
        market_data.append({
            "id": finn_id,
            "date": now,
            "title": l.get("title", ""),
            "price_kr": l.get("price_kr", 0),
            "score": l.get("score", 0),
            "specs": l.get("specs", ""),
            "url": l.get("url", ""),
        })
        if finn_id:
            existing_ids.add(finn_id)
    return market_data


# ---------------------------------------------------------------------------
# Finn.no scraping
# ---------------------------------------------------------------------------

def finn_id_from_url(url: str) -> str:
    m = re.search(r"/item/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"finnkode=(\d+)", url)
    return m.group(1) if m else ""


def _card_text(a_tag) -> str:
    card = a_tag
    for _ in range(8):
        parent = card.parent
        if parent is None:
            break
        if parent.name in ("article", "li"):
            card = parent
            break
        sibling_links = parent.find_all(
            "a", href=lambda h: h and "/recommerce/forsale/item/" in h
        )
        if len(sibling_links) > 1:
            break
        card = parent
    return card.get_text(separator=" ", strip=True)[:400]


def fetch_listings_from_page(url: str, params: dict = None) -> list[dict]:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    print(f"    HTTP {resp.status_code}, {len(resp.text)} bytes")

    soup = BeautifulSoup(resp.text, "html.parser")
    seen_ids: set[str] = set()
    listings = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/recommerce/forsale/item/" not in href and "finnkode=" not in href:
            continue
        fid = finn_id_from_url(href)
        if not fid or fid in seen_ids:
            continue
        seen_ids.add(fid)
        full_url = f"https://www.finn.no{href}" if href.startswith("/") else href
        listings.append({"id": fid, "url": full_url, "card_text": _card_text(a)})

    print(f"    {len(listings)} annonser fra siden")
    return listings


def collect_new_listings(seen: dict) -> list[dict]:
    all_listings: dict[str, dict] = {}

    for query in SEARCH_QUERIES:
        try:
            print(f"  Søker: '{query}'")
            for l in fetch_listings_from_page(FINN_SEARCH_URL, params={"q": query, "sort": "PUBLISHED_DESC"}):
                all_listings.setdefault(l["id"], l)
            time.sleep(3)
        except Exception as e:
            print(f"  Feil ved søk '{query}': {e}")

    new = {k: v for k, v in all_listings.items() if k not in seen}
    print(f"  {len(all_listings)} unike annonser funnet, {len(new)} nye")
    return list(new.values())


# ---------------------------------------------------------------------------
# Claude evaluation
# ---------------------------------------------------------------------------

def evaluate_batch(listings: list[dict], market_summary: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    market_section = f"\n{market_summary}\n" if market_summary else ""

    listings_block = "\n\n".join(
        f"ID: {l['id']}\nURL: {l['url']}\nTekst: {l.get('card_text', '(ingen tekst)')}"
        for l in listings
    )

    prompt = f"""{BUYER_PROFILE}
{market_section}
Evaluer følgende Finn.no-annonser. Hver annonse har ID, URL og kortekst hentet direkte fra siden.

{listings_block}

For HVER annonse returner ett JSON-objekt. Samle i ett JSON-array:
{{
  "id": "ID-feltet fra annonsen over — kopier nøyaktig",
  "title": "tittel fra tekstfeltet",
  "price": "pris som vist",
  "price_kr": 0,
  "location": "sted",
  "url": "URL-feltet fra annonsen over — kopier nøyaktig",
  "score": 0-100,
  "kupp": true|false,
  "specs": "dimensjoner og nøkkelspesifikasjoner hvis oppgitt",
  "sammendrag": "1-2 setninger — produkttype og prisvurdering",
  "advarsel": "evt. bekymring (feil størrelse, gipsvogn, for liten) eller ''",
  "shipping": false,
  "distance_ok": true
}}

price_kr: heltall i kroner, 0 hvis ikke oppgitt.
shipping: true hvis teksten nevner frakt/sending/levering.
distance_ok: true hvis sted er innenfor 1,5 t fra Oslo ELLER shipping er true.
Score: 80-100 kupp, 65-79 interessant, 50-64 akseptabelt, under 50 ikke relevant.
Hvis distance_ok false: gi score 0.

Returner KUN gyldig JSON-array — ett objekt per annonse, i samme rekkefølge."""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            print(f"    Ingen JSON-array i svar")
            return []
        results = json.loads(match.group(0))

        id_to_input = {l["id"]: l for l in listings}
        for r in results:
            src = id_to_input.get(r.get("id", ""))
            if src:
                r["id"] = src["id"]
                r["url"] = src["url"]

        return results
    except Exception as e:
        print(f"    Claude-feil: {e}")
        return []


def evaluate_listings(listings: list[dict], market_summary: str) -> list[dict]:
    if not listings:
        return []
    results = []
    for i in range(0, len(listings), EVAL_BATCH_SIZE):
        batch = listings[i:i + EVAL_BATCH_SIZE]
        print(f"  Batch {i // EVAL_BATCH_SIZE + 1}: evaluerer {len(batch)} annonser...")
        results.extend(evaluate_batch(batch, market_summary))
        if i + EVAL_BATCH_SIZE < len(listings):
            time.sleep(3)
    return results


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(listings: list[dict], market_data: list):
    kupps = [l for l in listings if l["score"] >= SCORE_KUPP]
    interesting = [l for l in listings if SCORE_THRESHOLD <= l["score"] < SCORE_KUPP]

    subject = (
        f"🛒 Finn.no transportvogn — {len(kupps)} kupp, {len(interesting)} interessante "
        f"({datetime.now().strftime('%d.%m %H:%M')})"
    )

    def render_card(l):
        badge_bg = "#c00" if l["score"] >= SCORE_KUPP else ("#2a7" if l["score"] >= 65 else "#888")
        location_tag = ""
        if l.get("shipping"):
            location_tag = ' <span style="font-size:11px;background:#e8f4e8;color:#2a7;border-radius:3px;padding:1px 5px;">📦 kan sendes</span>'
        specs_html = f'<span style="color:#999;"> · {l["specs"]}</span>' if l.get("specs") else ""
        warning = (
            f'<div style="margin-top:4px;font-size:12px;color:#c60;">⚠ {l["advarsel"]}</div>'
            if l.get("advarsel") else ""
        )
        return f"""
<div style="border:1px solid #ddd;border-radius:6px;padding:12px;margin-bottom:10px;">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
    <a href="{l['url']}" style="font-weight:bold;font-size:15px;color:#1a0dab;text-decoration:none;">{l['title']}</a>
    <span style="background:{badge_bg};color:#fff;border-radius:4px;padding:2px 8px;font-size:13px;white-space:nowrap;">{l['score']}</span>
  </div>
  <div style="margin-top:4px;font-size:14px;color:#444;">
    <strong>{l['price']}</strong> &nbsp;·&nbsp; {l['location']}{location_tag}{specs_html}
  </div>
  <div style="margin-top:6px;font-size:13px;color:#555;">{l['sammendrag']}</div>
  {warning}
</div>"""

    def render_section(title, color, items):
        if not items:
            return ""
        cards = "".join(render_card(l) for l in sorted(items, key=lambda x: -x["score"]))
        return f'<h3 style="color:{color};margin-top:24px;margin-bottom:8px;">{title} ({len(items)})</h3>{cards}'

    market_note = ""
    if market_data:
        prices = [i["price_kr"] for i in market_data if i.get("price_kr", 0) > 0]
        if prices:
            market_note = f'<p style="color:#888;font-size:12px;">Markedsdata: {len(market_data)} obs. · snitt {int(sum(prices)/len(prices)):,} kr · laveste {min(prices):,} kr</p>'

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px;">
<h2 style="border-bottom:2px solid #1a0dab;padding-bottom:8px;margin-bottom:4px;">Finn.no — Transportvogn</h2>
<p style="color:#888;font-size:13px;margin-top:0;">{datetime.now().strftime('%d. %B %Y, %H:%M')}</p>
{market_note}
{render_section("🏆 Kupp", "#c00", kupps)}
{render_section("✅ Interessant", "#2a7", [l for l in interesting if l['score'] >= 65])}
{render_section("📋 Akseptabelt", "#888", [l for l in interesting if l['score'] < 65])}
<p style="color:#ccc;font-size:11px;margin-top:32px;">finn-monitor · trolley</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["GMAIL_USER"]
    msg["To"] = os.environ["RECIPIENT_EMAIL"]
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        server.sendmail(os.environ["GMAIL_USER"], os.environ["RECIPIENT_EMAIL"], msg.as_string())

    print(f"E-post sendt: {subject}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen()
    market_data = load_market_data()
    market_summary = build_market_summary(market_data)

    if market_data:
        print(f"Markedsdata: {len(market_data)} tidligere annonser")

    print("Henter Finn.no søkeresultater...")
    new_listings = collect_new_listings(seen)

    if not new_listings:
        print("Ingen nye annonser — avslutter.")
        return

    print(f"Evaluerer {len(new_listings)} nye annonser med Claude...")
    evaluated = evaluate_listings(new_listings, market_summary)

    for l in evaluated:
        if not l.get("id"):
            l["id"] = finn_id_from_url(l.get("url", ""))

    market_data = append_to_market_data(market_data, evaluated)
    save_market_data(market_data)

    now = datetime.now().isoformat()
    seen_eval_ids: set[str] = set()
    deduped: list[dict] = []
    for l in evaluated:
        lid = l.get("id") or l.get("url", "")
        if lid and lid not in seen_eval_ids:
            seen_eval_ids.add(lid)
            deduped.append(l)
        key = l.get("id") or re.sub(r"[^a-z0-9]", "", l.get("url", ""))[:40]
        if key:
            seen[key] = {"date": now, "title": l.get("title", ""), "score": l.get("score", 0)}
    save_seen(seen)

    interesting = [l for l in deduped if l.get("score", 0) >= SCORE_THRESHOLD]
    print(f"{len(interesting)} over terskel (score ≥ {SCORE_THRESHOLD})")

    if interesting:
        send_email(interesting, market_data)
    else:
        print("Ingen relevante annonser å sende.")

    print("Ferdig!")


if __name__ == "__main__":
    main()
