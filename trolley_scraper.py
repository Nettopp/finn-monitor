import os
import json
import re
import smtplib
import time
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bs4 import BeautifulSoup
import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEEN_FILE = "seen_ids_trolley.json"
MARKET_FILE = "market_data_trolley.json"
SEEN_DAYS = 30
MARKET_MAX_ITEMS = 500

PASS1_BATCH_SIZE = 25   # card text — mange per batch
PASS2_BATCH_SIZE = 5    # full listing — færre per batch (mer tekst)
PASS1_THRESHOLD = 35    # minimum pass 1 score for å gå videre til full fetch
SCORE_THRESHOLD = 40    # minimum score etter pass 2 for å dukke opp i e-post

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

BUYER_PROFILE = """
Du evaluerer brukte transportvogner på Finn.no. Oppgaven er å finne store plattformvogner
av industrikvalitet som ligner referanseproduktet.

VIKTIG: PRIS PÅVIRKER IKKE SCOREN. Vi vil se alle relevante treff uansett pris.
Score baseres utelukkende på produktmatch.

REFERANSEPRODUKT — Transportvogn NIGEL (AJ Produkter):
  Type:         Stor, flat plattformvogn på 4 pneumatiske hjul
  Plattform:    Kryssfinér, 2000 × 1000 mm — vi søker 150–200 cm lang, 75–100 cm bred
  Kapasitet:    1500 kg
  Hjul:         Luftgummi Ø 406 mm, rullelager (firehjulsstyring, tohjulsstyring OK)
  Vekt:         86 kg — dette er en tung, solid vogn
  Typiske navn: transportvogn, plattformvogn, materialtralle, lagervogn, industrivogn

SCORING — kun basert på produktmatch:
  80–100  Klart treff: stor flat industriplattform på 4 hjul, tung konstruksjon, riktig størrelse
  60–79   Sannsynlig treff: riktig type, men usikker størrelse eller begrenset info
  40–59   Mulig treff: kan ikke utelukkes, men lite info
  0–39    Feil produkttype — ikke relevant

UTELUKK UMIDDELBART (score 0):
  - Hjultraller / rullestativ (for biler/motorsykler)
  - Serveringstraller / restaurantvogner / kafévogner
  - Jekketraller / pallekjerre / palle-jekk
  - Bagasjetraller / portertraller
  - Handlevogner / supermarkedsvogner
  - Kassevogner / posttraller / arkivvogner
  - Sykkeltraller / lastesykler
  - Gipsvogner med hev/senk-mekanisme
  - Lette billigtraller (Clas Ohlson, Jula, IKEA, Biltema) — for små og lette
  - Rullestillaser
  - Alt annet som åpenbart ikke er en stor industriplattformvogn

LOKASJON:
  Innenfor 1,5 t fra Oslo: Østfold, Vestfold (Tønsberg/Sandefjord), Kongsberg, Hamar, Gjøvik, Hønefoss → OK
  Utenfor (Bergen, Stavanger, Trondheim, Kristiansand, Tromsø, Bodø) → score 0
  Unntak: "kan sendes", "frakt", "sender", "levering" i teksten → lokasjon ignoreres

Oppgi pris i price/price_kr-feltene for info, men la det IKKE påvirke scoren.
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


def _seen_key(listing: dict) -> str:
    """Finn item ID is the canonical dedup key — stable across URL variants."""
    return listing.get("id") or listing.get("url", "")


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


def _card_text(a_tag, max_chars: int = 800) -> str:
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
    return card.get_text(separator=" ", strip=True)[:max_chars]


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


def fetch_full_listing(url: str) -> str:
    """Henter full annonseside og returnerer renset tekst (maks 3000 tegn)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "head", "header"]):
            tag.decompose()

        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(attrs={"id": re.compile(r"main|content|listing", re.I)})
            or soup.body
        )
        text = (main or soup).get_text(separator=" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()[:3000]
    except Exception as e:
        return f"(kunne ikke hente side: {e})"


def collect_new_listings(seen: dict) -> list[dict]:
    all_listings: dict[str, dict] = {}

    for query in SEARCH_QUERIES:
        try:
            print(f"  Søker: '{query}'")
            for l in fetch_listings_from_page(FINN_SEARCH_URL, params={"q": query, "sort": "PUBLISHED_DESC"}):
                key = _seen_key(l)
                if key:
                    all_listings.setdefault(key, l)
            time.sleep(3)
        except Exception as e:
            print(f"  Feil ved søk '{query}': {e}")

    new = {k: v for k, v in all_listings.items() if k not in seen}
    print(f"  {len(all_listings)} unike annonser funnet, {len(new)} nye")
    return list(new.values())


# ---------------------------------------------------------------------------
# Claude evaluation
# ---------------------------------------------------------------------------

def _build_prompt(listings: list[dict], market_summary: str, text_field: str, context_note: str) -> str:
    market_section = f"\n{market_summary}\n" if market_summary else ""
    listings_block = "\n\n".join(
        f"ID: {l['id']}\nURL: {l['url']}\nTekst: {l.get(text_field, '(ingen tekst)')}"
        for l in listings
    )
    return f"""{BUYER_PROFILE}
{market_section}
{context_note}
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
  "specs": "dimensjoner og nøkkelspesifikasjoner hvis oppgitt",
  "sammendrag": "1-2 setninger — hva slags vogn er dette og hvorfor passer/passer ikke",
  "advarsel": "konkret bekymring (feil type, feil størrelse, gipsvogn) eller ''",
  "shipping": false,
  "distance_ok": true
}}

price_kr: heltall i kroner, 0 hvis ikke oppgitt.
shipping: true hvis teksten nevner frakt/sending/levering.
distance_ok: true hvis sted er innenfor 1,5 t fra Oslo ELLER shipping er true.
Hvis distance_ok false: gi score 0.

Returner KUN gyldig JSON-array — ett objekt per annonse, i samme rekkefølge."""


def _call_claude(prompt: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            print("    Ingen JSON-array i svar")
            return []
        return json.loads(match.group(0))
    except Exception as e:
        print(f"    Claude-feil: {e}")
        return []


def _fix_ids(results: list[dict], listings: list[dict]) -> list[dict]:
    """Erstatt Claude-genererte IDer/URLer med de faktiske input-verdiene."""
    id_to_input = {l["id"]: l for l in listings}
    for r in results:
        src = id_to_input.get(r.get("id", ""))
        if src:
            r["id"] = src["id"]
            r["url"] = src["url"]
    return results


def evaluate_pass1(listings: list[dict], market_summary: str) -> list[dict]:
    """Pass 1: rask evaluering med korttekst fra søkeresultatsiden."""
    results = []
    note = "Evaluer følgende Finn.no-annonser basert på kortekst fra søkeresultatsiden.\n"
    for i in range(0, len(listings), PASS1_BATCH_SIZE):
        batch = listings[i:i + PASS1_BATCH_SIZE]
        print(f"  Pass 1 batch {i // PASS1_BATCH_SIZE + 1}: {len(batch)} annonser...")
        prompt = _build_prompt(batch, market_summary, "card_text", note)
        r = _fix_ids(_call_claude(prompt), batch)
        results.extend(r)
        if i + PASS1_BATCH_SIZE < len(listings):
            time.sleep(2)
    return results


def evaluate_pass2(candidates: list[dict], market_summary: str) -> list[dict]:
    """Pass 2: dyp evaluering med full annonseside for kandidater fra pass 1."""
    print(f"  Henter fulle annonsesider for {len(candidates)} kandidater...")
    for l in candidates:
        l["full_text"] = fetch_full_listing(l["url"])
        time.sleep(1.5)

    results = []
    note = (
        "Du har nå FULL TEKST fra selve annonsesiden (ikke bare søkekortet). "
        "Vær mer presis i evalueringen — du har nok info til å avgjøre produkttype, "
        "størrelse og stand.\n"
        "Evaluer følgende kandidater:\n"
    )
    for i in range(0, len(candidates), PASS2_BATCH_SIZE):
        batch = candidates[i:i + PASS2_BATCH_SIZE]
        print(f"  Pass 2 batch {i // PASS2_BATCH_SIZE + 1}: {len(batch)} annonser...")
        prompt = _build_prompt(batch, market_summary, "full_text", note)
        r = _fix_ids(_call_claude(prompt), batch)
        results.extend(r)
        if i + PASS2_BATCH_SIZE < len(candidates):
            time.sleep(2)
    return results


def evaluate_two_pass(
    new_listings: list[dict], market_summary: str
) -> tuple[list[dict], list[dict]]:
    """
    Returnerer (alle_pass1_resultater, pass2_resultater_over_terskel).
    Alle pass1-resultater lagres i seen_ids uansett score.
    """
    if not new_listings:
        return [], []

    print(f"Pass 1: evaluerer {len(new_listings)} annonser med korttekst...")
    pass1 = evaluate_pass1(new_listings, market_summary)

    candidates_input = {l["id"]: l for l in new_listings}
    candidates = [
        candidates_input[r["id"]]
        for r in pass1
        if r.get("score", 0) >= PASS1_THRESHOLD and r.get("id") in candidates_input
    ]
    print(f"Pass 1: {len(candidates)}/{len(new_listings)} videre til pass 2 (score ≥ {PASS1_THRESHOLD})")

    if not candidates:
        return pass1, []

    print(f"Pass 2: henter fulle annonsesider og re-evaluerer...")
    pass2 = evaluate_pass2(candidates, market_summary)

    final = [r for r in pass2 if r.get("score", 0) >= SCORE_THRESHOLD]
    print(f"Pass 2: {len(final)} over terskel (score ≥ {SCORE_THRESHOLD})")
    return pass1, final


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(listings: list[dict], market_data: list):
    high = [l for l in listings if l["score"] >= 80]
    mid  = [l for l in listings if 65 <= l["score"] < 80]
    low  = [l for l in listings if SCORE_THRESHOLD <= l["score"] < 65]

    subject = (
        f"🛒 Finn.no transportvogn — {len(listings)} treff "
        f"({datetime.now().strftime('%d.%m %H:%M')})"
    )

    def render_card(l):
        score = l["score"]
        badge_bg = "#2a7" if score >= 80 else ("#e67e00" if score >= 65 else "#888")
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
    <span style="background:{badge_bg};color:#fff;border-radius:4px;padding:2px 8px;font-size:13px;white-space:nowrap;">{score}</span>
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
{render_section("✅ Klart treff", "#2a7", high)}
{render_section("🔍 Sannsynlig treff", "#e67e00", mid)}
{render_section("❓ Mulig treff", "#888", low)}
<p style="color:#ccc;font-size:11px;margin-top:32px;">finn-monitor · trolley · to-pass evaluering</p>
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

    pass1_results, final_results = evaluate_two_pass(new_listings, market_summary)

    # Alle pass1-resultater lagres i seen (uansett score) — evalueres ikke igjen
    now = datetime.now().isoformat()
    pass2_by_id = {r["id"]: r for r in final_results if r.get("id")}

    seen_keys: set[str] = set()
    all_for_market: list[dict] = []
    for r in pass1_results:
        key = _seen_key(r)
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        # Bruk pass2-score hvis tilgjengelig, ellers pass1
        best = pass2_by_id.get(r.get("id", ""), r)
        seen[key] = {"date": now, "title": best.get("title", ""), "score": best.get("score", 0)}
        all_for_market.append(best)

    market_data = append_to_market_data(market_data, all_for_market)
    save_market_data(market_data)
    save_seen(seen)

    if final_results:
        send_email(final_results, market_data)
    else:
        print("Ingen relevante annonser å sende.")

    print("Ferdig!")


if __name__ == "__main__":
    main()
