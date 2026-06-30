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

SEEN_FILE = "seen_ids.json"
MARKET_FILE = "market_data.json"
SEEN_DAYS = 30
MARKET_MAX_ITEMS = 500
NEW_LISTINGS_CAP = None  # no cap — process all new listings
EVAL_BATCH_SIZE = 25    # listings per Claude call
SCORE_THRESHOLD = 80
SCORE_KUPP = 80

FINN_SEARCH_URL = "https://www.finn.no/recommerce/forsale/search"
SEARCH_QUERIES = []

# Torget > Sport og friluftsliv > Vannsport > Wingfoil, filtrert på Horten og Tønsberg
# OBS: location-kodene kan trenge justering — verifiser ved å gjøre et manuelt søk på finn.no
# og kopiere URL-en med ønsket region valgt.
CATEGORY_URLS = [
    "https://www.finn.no/recommerce/forsale/search?location=1.20008.20131&location=1.20008.20133&location=1.20008.20132&product_category=2.69.7738.2467&sort=PUBLISHED_DESC",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BUYER_PROFILE = """
Evaluer brukt vannsportutstyr (wingfoil-kategorien) til salgs på Finn.no.
Ingen restriksjoner på produkttype, størrelse eller årsmodell — alt i kategorien er aktuelt.

LOKASJON:
  Kun annonser fra Horten eller Tønsberg-regionen er aktuelle.
  Annonser fra andre steder i Norge: gi score 0.
  Unntak: hvis teksten inneholder ord som "kan sendes", "frakt", "sender" eller "levering",
  er lokasjon irrelevant og skal ikke trekke ned.

PRISVURDERING — score basert på pris vs. antatt markedsverdi:
  80–100 (kupp): klart underpriset for merke og stand — kjent merke, bra tilstand, lav pris.
                 Vær streng: 80+ skal bety et genuint godt kjøp som det haster å handle.
  60–79: OK pris, men ikke kupp.
  Under 60: normal pris, overpriset, eller for lite info til å vurdere.

Vi varsles KUN om kupp (score ≥ 80) — sett terskelen høyt.
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

    by_cat = defaultdict(list)
    for item in market_data:
        cat = item.get("kategori", "annet")
        if cat != "annet":
            by_cat[cat].append(item)

    if not by_cat:
        return ""

    lines = [
        "Markedsdata fra tidligere observerte Finn.no-annonser "
        "(bruk som referanse for prisvurdering av nye annonser):"
    ]

    for cat in ["vinge", "foil", "brett", "komplett"]:
        items = by_cat.get(cat)
        if not items:
            continue
        prices = [i["price_kr"] for i in items if i.get("price_kr", 0) > 0]
        if not prices:
            continue
        avg = int(sum(prices) / len(prices))
        low = min(prices)
        best = sorted(items, key=lambda x: -x.get("score", 0))[:4]
        lines.append(
            f"\n{cat.upper()} — {len(items)} obs. | snitt {avg:,} kr | laveste {low:,} kr"
        )
        for b in best:
            specs = f", {b['specs']}" if b.get("specs") else ""
            lines.append(f"    · {b['title']} — {b['price_kr']:,} kr (score {b['score']}{specs})")

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
            "kategori": l.get("kategori", "annet"),
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
    """Walk up the DOM from a listing link to get the card's text content."""
    card = a_tag
    for _ in range(8):
        parent = card.parent
        if parent is None:
            break
        if parent.name in ("article", "li"):
            card = parent
            break
        # Stop before a container that holds multiple listing links
        sibling_links = parent.find_all(
            "a", href=lambda h: h and "/recommerce/forsale/item/" in h
        )
        if len(sibling_links) > 1:
            break
        card = parent
    return card.get_text(separator=" ", strip=True)[:400]


def fetch_listings_from_page(url: str, params: dict = None) -> list[dict]:
    """
    Fetch one Finn.no search page.
    Returns list of dicts: {id, url, card_text} — one per listing, deduplicated by id.
    """
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
    """
    Scrape all searches and category pages.
    Returns list of new listing dicts ({id, url, card_text}) not in seen_ids.
    """
    all_listings: dict[str, dict] = {}  # finn_id → listing dict

    for query in SEARCH_QUERIES:
        try:
            for l in fetch_listings_from_page(FINN_SEARCH_URL, params={"q": query, "sort": "PUBLISHED_DESC"}):
                all_listings.setdefault(l["id"], l)
            time.sleep(3)
        except Exception as e:
            print(f"  Feil ved søk '{query}': {e}")

    for cat_url in CATEGORY_URLS:
        try:
            for l in fetch_listings_from_page(cat_url):
                all_listings.setdefault(l["id"], l)
            time.sleep(3)
        except Exception as e:
            print(f"  Feil ved kategori-URL '{cat_url}': {e}")

    new = {k: v for k, v in all_listings.items() if k not in seen}
    print(f"  {len(all_listings)} unike annonser funnet, {len(new)} nye")

    if NEW_LISTINGS_CAP and len(new) > NEW_LISTINGS_CAP:
        new = dict(list(new.items())[:NEW_LISTINGS_CAP])
        print(f"  Begrenset til {NEW_LISTINGS_CAP} per kjøring (cap aktiv)")

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
  "url": "URL-feltet fra annonsen over — kopier nøyaktig, IKKE bytt med annen annonse",
  "score": 0-100,
  "kategori": "vinge|foil|brett|komplett|annet",
  "kupp": true|false,
  "specs": "nøkkelspesifikasjoner",
  "sammendrag": "1-2 setninger — utstyr og prisvurdering",
  "advarsel": "evt. bekymring eller ''",
  "shipping": false,
  "distance_ok": true
}}

price_kr: heltall i kroner, 0 hvis ikke oppgitt.
shipping: true hvis teksten nevner frakt/sending/levering.
distance_ok: true hvis sted er innenfor 1,5 t fra Oslo ELLER shipping er true.
Score: 80-100 kupp, 60-79 interessant, under 60 marginal/ikke relevant.
Hvis distance_ok false: trekk 25 poeng og nevn i advarsel.

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

        # Guarantee URL and ID are always taken from input, not Claude's guess
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
        f"🏆 Finn.no wingfoil — {len(kupps)} kupp, {len(interesting)} interessante "
        f"({datetime.now().strftime('%d.%m %H:%M')})"
    )

    def market_stats_html() -> str:
        if not market_data:
            return ""
        by_cat = defaultdict(list)
        for item in market_data:
            cat = item.get("kategori", "")
            if cat and cat != "annet":
                by_cat[cat].append(item)
        rows = ""
        for cat in ["vinge", "foil", "brett", "komplett"]:
            items = by_cat.get(cat, [])
            prices = [i["price_kr"] for i in items if i.get("price_kr", 0) > 0]
            if not prices:
                continue
            avg = int(sum(prices) / len(prices))
            rows += (
                f"<tr><td style='padding:2px 10px 2px 0;color:#555;'>{cat}</td>"
                f"<td style='padding:2px 10px 2px 0;'>{len(items)}</td>"
                f"<td style='padding:2px 10px 2px 0;'>{avg:,} kr</td>"
                f"<td style='padding:2px 0;'>{min(prices):,} kr</td></tr>"
            )
        if not rows:
            return ""
        return f"""
<div style="margin-top:24px;">
  <h4 style="margin:0 0 6px;color:#888;font-size:12px;text-transform:uppercase;">
    Markedsdata ({len(market_data)} obs.)
  </h4>
  <table style="font-size:12px;border-collapse:collapse;">
    <tr style="color:#aaa;font-size:11px;">
      <th align="left" style="padding:0 10px 4px 0;">Kategori</th>
      <th align="left" style="padding:0 10px 4px 0;">Obs.</th>
      <th align="left" style="padding:0 10px 4px 0;">Snitt</th>
      <th align="left">Laveste</th>
    </tr>{rows}
  </table>
</div>"""

    def render_card(l):
        badge_bg = "#c00" if l["score"] >= SCORE_KUPP else "#2a7"
        location_tag = ""
        if l.get("shipping"):
            location_tag = ' <span style="font-size:11px;background:#e8f4e8;color:#2a7;border-radius:3px;padding:1px 5px;">📦 kan sendes</span>'
        elif not l.get("distance_ok", True):
            location_tag = ' <span style="font-size:11px;background:#fef0e6;color:#c60;border-radius:3px;padding:1px 5px;">📍 for langt unna</span>'
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
    <strong>{l['price']}</strong> &nbsp;·&nbsp; {l['location']}{location_tag}
    &nbsp;·&nbsp; <em style="color:#888;">{l['kategori']}</em>{specs_html}
  </div>
  <div style="margin-top:6px;font-size:13px;color:#555;">{l['sammendrag']}</div>
  {warning}
</div>"""

    def render_section(title, color, items):
        if not items:
            return ""
        cards = "".join(render_card(l) for l in sorted(items, key=lambda x: -x["score"]))
        return f'<h3 style="color:{color};margin-top:24px;margin-bottom:8px;">{title} ({len(items)})</h3>{cards}'

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px;">
<h2 style="border-bottom:2px solid #1a0dab;padding-bottom:8px;margin-bottom:4px;">Finn.no wingfoil</h2>
<p style="color:#888;font-size:13px;margin-top:0;">{datetime.now().strftime('%d. %B %Y, %H:%M')}</p>
{render_section("🏆 Kupp", "#c00", kupps)}
{render_section("✅ Interessant", "#2a7", interesting)}
{market_stats_html()}
<p style="color:#ccc;font-size:11px;margin-top:32px;">finn-monitor</p>
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

    # Enrich with IDs from URLs if Claude didn't fill them in
    for l in evaluated:
        if not l.get("id"):
            l["id"] = finn_id_from_url(l.get("url", ""))

    # Update market data and seen IDs
    market_data = append_to_market_data(market_data, evaluated)
    save_market_data(market_data)

    now = datetime.now().isoformat()
    for l in evaluated:
        key = l.get("id") or re.sub(r"[^a-z0-9]", "", l.get("url", ""))[:40]
        if key:
            seen[key] = {"date": now, "title": l.get("title", ""), "score": l.get("score", 0)}
    save_seen(seen)

    # Deduplicate by ID (same listing can appear in multiple searches)
    seen_eval_ids: set[str] = set()
    deduped: list[dict] = []
    for l in evaluated:
        lid = l.get("id") or l.get("url", "")
        if lid and lid not in seen_eval_ids:
            seen_eval_ids.add(lid)
            deduped.append(l)
    evaluated = deduped

    interesting = [l for l in evaluated if l.get("score", 0) >= SCORE_THRESHOLD]
    print(f"{len(interesting)} over terskel (score ≥ {SCORE_THRESHOLD})")

    if interesting:
        send_email(interesting, market_data)
    else:
        print("Ingen interessante annonser å sende.")

    print("Ferdig!")


if __name__ == "__main__":
    main()
