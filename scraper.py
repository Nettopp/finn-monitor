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
SCORE_THRESHOLD = 60
SCORE_KUPP = 80

FINN_SEARCH_URL = "https://www.finn.no/recommerce/forsale/search"
SEARCH_QUERIES = ["wingfoil", "wing foil"]

# Category browse: Torget > Sport og friluftsliv > Vannsport > Wingfoil
CATEGORY_URLS = [
    "https://www.finn.no/recommerce/forsale/search?product_category=2.69.7738.2467&sort=PUBLISHED_DESC",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BUYER_PROFILE = """
Kjøperprofil:
- Vekt: 88 kg, høyde: 187 cm
- Nybegynner wingfoil, erfaren snowboard/jolle-seiler
- Lokasjon: Indre Oslofjord (Borre/Horten), typisk 5-8 m/s vind
- Kjøper brukt utstyr, ønsker ikke bruke for mye

Søker (i prioritert rekkefølge):

VINGE: 5.5-7 m², kjente merker (Duotone, Cabrinha, F-One, North, Ozone, Naish).
  God pris: under 8 000 kr. Kupp: under 5 000 kr. Unngå ukjente kinesiske merker.

FOIL komplett (front-wing + mast + fuselage): front-wing 950-1 800 cm², mast 60-80 cm.
  Kjente merker: Duotone, Armstrong, Cabrinha, F-One, Fanatic, North, Slingshot.
  God pris: under 8 000 kr komplett. Kupp: under 5 000 kr.

BRETT: 130-170L, bredde 68-80 cm.
  Kjente merker: Fanatic, Duotone, F-One, North, JP Australia.
  God pris: under 6 000 kr. Kupp: under 3 500 kr.

KOMPLETT SETT (vinge + foil + brett):
  God pris: under 18 000 kr. Kupp: under 12 000 kr.

LOKASJON:
  Kjøper kan hente innenfor ca. 1,5 times kjøring fra Oslo sentrum.
  Dette dekker omtrent: Østfold, Vestfold (Tønsberg/Sandefjord), Kongsberg, Hamar, Gjøvik, Hønefoss.
  Utenfor rekkevidde (eksempler): Bergen, Stavanger, Trondheim, Kristiansand, Ålesund, Tromsø, Bodø.
  Hvis annonsen indikerer at produktet kan sendes (ord som "kan sendes", "frakt", "sender", "levering"),
  er lokasjon irrelevant — trekk ikke fra for avstand.
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


def fetch_page(url: str, params: dict = None) -> tuple[str, list[str]]:
    """Fetch a Finn.no page. Returns (page text, list of ad URLs)."""
    resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/recommerce/forsale/item/" in href or "finnkode=" in href:
            full = f"https://www.finn.no{href}" if href.startswith("/") else href
            urls.append(full)

    text = soup.get_text(separator="\n", strip=True)[:5000]
    return text, list(dict.fromkeys(urls))


def collect_new_listings(seen: dict) -> tuple[list[dict], list[str]]:
    """
    Scrape all search queries and category URLs, collect unique URLs not yet in seen_ids.
    Returns list of raw listing dicts (url + id) and the full page texts
    for batched Claude evaluation.
    """
    all_urls: dict[str, str] = {}   # finn_id/key → url
    page_texts: list[str] = []

    for query in SEARCH_QUERIES:
        try:
            text, urls = fetch_page(FINN_SEARCH_URL, params={"q": query, "sort": "PUBLISHED_DESC"})
            page_texts.append(f"Søk: {query}\n{text}")
            for url in urls:
                fid = finn_id_from_url(url)
                key = fid or url
                if key not in all_urls:
                    all_urls[key] = url
            time.sleep(1)
        except Exception as e:
            print(f"  Feil ved søk '{query}': {e}")

    for cat_url in CATEGORY_URLS:
        try:
            text, urls = fetch_page(cat_url)
            page_texts.append(f"Kategori: {cat_url}\n{text}")
            for url in urls:
                fid = finn_id_from_url(url)
                key = fid or url
                if key not in all_urls:
                    all_urls[key] = url
            time.sleep(1)
        except Exception as e:
            print(f"  Feil ved kategori-URL '{cat_url}': {e}")

    new_urls = {k: v for k, v in all_urls.items() if k not in seen}
    print(f"  {len(all_urls)} unike annonser funnet, {len(new_urls)} nye")

    listings = [{"id": k if k.isdigit() else "", "url": v} for k, v in new_urls.items()]
    return listings, page_texts


# ---------------------------------------------------------------------------
# Claude evaluation
# ---------------------------------------------------------------------------

def evaluate_listings(listings: list[dict], page_texts: list[str], market_summary: str) -> list[dict]:
    if not listings:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    market_section = f"\n{market_summary}\n" if market_summary else ""
    urls_block = "\n".join(l["url"] for l in listings[:40])
    pages_block = "\n\n---\n\n".join(page_texts)[:6000]

    prompt = f"""{BUYER_PROFILE}
{market_section}
Følgende annonser ble funnet på Finn.no (lagrede søk for wingfoil-utstyr):

Lenker til nye annonser:
{urls_block}

Søkeresultatsider (for kontekst — tittel, pris, sted er synlig her):
{pages_block}

Oppgave:
Evaluer HVER av annonsene i lenkelisten mot kjøperprofilen.
Bruk søkesidene for å finne informasjon om tittel, pris og sted.
Bruk markedsdataene for prisvurdering.

Returner et JSON-array. Hvert objekt:
{{
  "id": "finn-id (tall fra finnkode= i URL, eller '' hvis ikke funnet)",
  "title": "tittel",
  "price": "pris som vist",
  "price_kr": 0,
  "location": "sted",
  "url": "finn.no URL",
  "score": 0-100,
  "kategori": "vinge|foil|brett|komplett|annet",
  "kupp": true|false,
  "specs": "nøkkelspesifikasjoner, f.eks. '6m, 2022, god stand'",
  "sammendrag": "1-2 setninger — utstyr og prisvurdering vs. markedssnitt",
  "advarsel": "evt. bekymring eller ''",
  "shipping": false,
  "distance_ok": true
}}

price_kr: pris som heltall i kroner. 0 hvis ikke oppgitt.
shipping: true hvis annonsen nevner frakt/sending/levering.
distance_ok: true hvis sted er innenfor 1,5 t fra Oslo, ELLER shipping er true.

Score:
- 80-100: Kupp
- 60-79: Interessant
- 40-59: Marginal
- Under 40: Ikke relevant
- Hvis distance_ok er false: trekk 25 poeng fra og nevn i advarsel

Returner KUN gyldig JSON-array."""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        print(f"  Claude-feil: {e}")
        return []


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
    new_listings, page_texts = collect_new_listings(seen)

    if not new_listings:
        print("Ingen nye annonser — avslutter.")
        return

    print(f"Evaluerer {len(new_listings)} nye annonser med Claude...")
    evaluated = evaluate_listings(new_listings, page_texts, market_summary)

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

    interesting = [l for l in evaluated if l.get("score", 0) >= SCORE_THRESHOLD]
    print(f"{len(interesting)} over terskel (score ≥ {SCORE_THRESHOLD})")

    if interesting:
        send_email(interesting, market_data)
    else:
        print("Ingen interessante annonser å sende.")

    print("Ferdig!")


if __name__ == "__main__":
    main()
