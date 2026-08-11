"""
AI-nieuwsscan in een keer: feeds ophalen, AI-artikelen filteren en de website
bouwen of aanvullen.

Dit script bundelt de stappen die eerder verdeeld zaten over rss_reader.py
(ophalen) en build_ai_page.py (filteren + HTML). Het verschil met de oude
aanpak: de website is cumulatief. Bestaat er al een pagina, dan blijven alle
eerdere artikelen staan en worden enkel de ontbrekende nieuwe items toegevoegd.

Dit bestand staat op zichzelf: het heeft geen andere scripts nodig en gebruikt
enkel de Python standaardbibliotheek. Feeds toevoegen of weghalen doe je in
FEEDS en TOPIC_PAGES hieronder.

Gebruik:
    python ai_nieuwsscan.py
    python ai_nieuwsscan.py --days 14
    python ai_nieuwsscan.py --out docs/index.html
    python ai_nieuwsscan.py --archive          # bewaart ook de ruwe .txt per feed
    python ai_nieuwsscan.py --dry-run          # toont wat er bij zou komen
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TODAY = date.today()

# De site en het archief horen bij het script, niet bij de map van waaruit je
# het toevallig start. Zo bouwt een geplande taak altijd dezelfde site verder.
SCRIPT_DIR = Path(__file__).resolve().parent

# De pagina staat in docs/, de map die GitHub Pages publiceert.
DEFAULT_OUT = "docs/index.html"
DEFAULT_DATA = "ai_artikelen.json"
DEFAULT_DAYS = 7

# Hoeveel dagen de pagina toont. Het archief in het JSON-bestand houdt alles
# bij, ook wat van de pagina verdwijnt.
PAGE_DAYS = 31

# Aantal extra pogingen per bron bij een netwerkfout, en de basiswachttijd
# (die verdubbelt per poging: 0,5 - 1 - 2 - 4 seconden).
FETCH_RETRIES = 4
FETCH_BACKOFF = 0.5

# De RSS- en Atom-feeds. De sleutel bepaalt het label op de site: de prefix
# wordt vertaald via PREFIX_NAMES, de rest wordt het sectielabel.
FEEDS = {
    "standaard_binnenland": "https://www.standaard.be/binnenland/rss/",
    "standaard_buitenland": "https://www.standaard.be/buitenland/rss/",
    "standaard_cultuur": "https://www.standaard.be/media-en-cultuur/rss/",
    "standaard_economie": "https://www.standaard.be/economie/rss/",
    "standaard_sport": "https://www.standaard.be/sport/rss/",
    "standaard_biz_mijn_geld": "https://www.standaard.be/economie/geld/rss/",
    "standaard_biz_financiele_markten": "https://www.standaard.be/economie/beurs/rss/",
    "standaard_opinies": "https://www.standaard.be/opinies/rss/",
    "apache_artikels": "https://apache.be/feed/artikels",
    "demorgen_in_het_nieuws": "https://www.demorgen.be/in-het-nieuws/rss.xml",
    "demorgen_beter_leven": "https://www.demorgen.be/beter-leven/rss.xml",
    "demorgen_koken": "https://www.demorgen.be/koken-met-de-morgen/rss.xml",
    "demorgen_meningen": "https://www.demorgen.be/meningen/rss.xml",
    "demorgen_oorlog_oekraine": "https://www.demorgen.be/oorlog-in-oekraine/rss.xml",
    "demorgen_politiek": "https://www.demorgen.be/politiek/rss.xml",
    "demorgen_wetenschap": "https://www.demorgen.be/tech-wetenschap/rss.xml",
    "demorgen_sport": "https://www.demorgen.be/sport/rss.xml",
    "demorgen_luister": "https://www.demorgen.be/luister/rss.xml",
    "demorgen_cartoons": "https://www.demorgen.be/puzzels-cartoons/rss.xml",
    "demorgen_best_gelezen": "https://www.demorgen.be/popular/rss.xml",
    "tijd_nieuws": "https://www.tijd.be/rss/nieuws.xml",
    "tijd_ondernemen_ict": "https://www.tijd.be/rss/ondernemen_ict.xml",
    "tijd_opinie": "https://www.tijd.be/rss/opinie.xml",
    "tijd_cultuur": "https://www.tijd.be/rss/cultuur.xml",
    "tijd_politiek": "https://www.tijd.be/rss/politiek.xml",
    "tijd_politiek_belgie": "https://www.tijd.be/rss/politiek_belgie.xml",
    "tijd_politiek_belgie_brussel": "https://www.tijd.be/rss/politiek_belgie_brussel.xml",
    "tijd_politiek_europa": "https://www.tijd.be/rss/politiek_europa.xml",
    "tijd_politiek_internationaal": "https://www.tijd.be/rss/politiek_internationaal.xml",
    "knack_artikels": "https://www.knack.be/feed/",
    "vrt_artikels": "https://www.vrt.be/vrtnws/nl.rss.articles.xml",
    "datanews_artikels": "https://trends.knack.be/feed/?host=datanews.knack.be",
    "humo_vandaag": "https://www.humo.be/vandaag/rss.xml",
    # Nederlandse bronnen. Let op: de Tweakers-feed via feedburner.com is
    # blijven steken in april 2025, deze op tweakers.net zelf is actueel.
    "tweakers_mixed": "https://tweakers.net/feeds/mixed.xml",
    "nos_tech": "https://feeds.nos.nl/nosnieuwstech",
    "nu_tech": "https://www.nu.nl/rss/tech",
    "nrc_artikels": "https://www.nrc.nl/rss/",
    "trouw_voorpagina": "https://www.trouw.nl/voorpagina/rss.xml",
    "trouw_wetenschap": "https://www.trouw.nl/wetenschap/rss.xml",
    "trouw_opinie": "https://www.trouw.nl/opinie/rss.xml",
}

# Bronnen zonder feed, maar met een gewone webpagina die de artikellijst als
# ingebedde JSON meelevert (Next.js __NEXT_DATA__). Per bron het maximum
# aantal artikelen dat we eruit halen.
TOPIC_PAGES = {
    "vrt_artificiele_intelligentie": (
        "https://www.vrt.be/vrtnws/nl/net-binnen/technologie-en-wetenschap/artificiele-intelligentie/",
        10,
    ),
}

USER_AGENT = "Mozilla/5.0 (AI-nieuwsscan)"

# Feeds die vroeger apart bestonden maar dezelfde URL ophaalden. Artikelen uit
# het archief krijgen de overgebleven naam, zodat er geen kaart met drie keer
# dezelfde bron als badge blijft staan.
REPLACED_FEEDS = {
    "De Standaard - Media": "De Standaard - Cultuur",
    "De Standaard - Biz Overzicht": "De Standaard - Economie",
    "De Standaard - Biz Economie": "De Standaard - Economie",
}

PREFIX_NAMES = {
    "standaard_": "De Standaard",
    "tijd_": "De Tijd",
    "demorgen_": "De Morgen",
    "vrt_": "VRT NWS",
    "apache_": "Apache.be",
    "datanews_": "Data News",
    "knack_": "Knack",
    "humo_": "Humo",
    "tweakers_": "Tweakers",
    "nos_": "NOS",
    "nu_": "NU.nl",
    "nrc_": "NRC",
    "trouw_": "Trouw",
}

AI_KEYWORDS = [
    # Algemene begrippen
    r"\bAI\b",
    r"kunstmatige intelligentie",
    r"artifici[eë]le intelligentie",
    r"machine\s*learning",
    r"deep\s*learning",
    r"generatieve ai",
    r"neuraal netwerk",
    r"algoritm",              # vangt ook algoritmes en algoritmisch
    r"taalmodel",
    r"large language model",
    r"\bllm\b",
    r"\bagi\b",
    r"superintelligentie",
    r"chatbot",

    # Bedrijven en modellen. Specifieke namen, dus nauwelijks valse treffers,
    # en ze vangen artikelen die het woord AI zelf nooit gebruiken.
    r"openai",
    r"ChatGPT",
    r"\bgpt[-\s]?\d",
    r"anthropic",
    r"claude ai",
    r"google gemini",
    r"\bgemini\b",
    r"deepmind",
    r"\bgrok\b",
    r"\bxai\b",
    r"\bmistral\b",
    r"\bllama\b",
    r"perplexity",
    r"midjourney",
    r"\bsora\b",
    r"stable diffusion",
    r"hugging face",
    r"\bcopilot\b",

    # Infrastructuur: de hardware en de datacenters achter AI. Dit is een
    # apart verhaal in het nieuws, vaak zonder dat het woord AI valt.
    r"\bnvidia\b",
    r"\btsmc\b",
    r"\basml\b",
    r"\bamd\b",
    r"halfgeleider",
    r"semiconductor",
    r"chip(fabrikant|maker|sector|reus|industrie|machine|producent|bedrijf|ontwerper|tekort)",
    r"datacenter|datacentrum|datacentra",
    r"\bgpu'?s?\b",
    r"rekenkracht",

    # Toepassingen en gevolgen
    r"deepfake",
    r"gezichtsherkenning",
    r"robot(ica)?\b",
    r"zelfrijdende",
]
AI_PATTERN = re.compile("|".join(AI_KEYWORDS), re.IGNORECASE)

# Context-gebonden uitzonderingen voor bekende valse treffers (kunstenaar Ai
# Weiwei, de stad Grok in koppen over sport, enz.).
FALSE_POSITIVE_PATTERNS = [
    re.compile(r"ai\s+weiwei", re.IGNORECASE),
]

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref"}


# --------------------------------------------------------------------------
# Filteren
# --------------------------------------------------------------------------

def is_false_positive(text, match):
    for fp in FALSE_POSITIVE_PATTERNS:
        for fp_match in fp.finditer(text):
            if fp_match.start() <= match.start() < fp_match.end():
                return True
    return False


def has_real_ai_match(text):
    for m in AI_PATTERN.finditer(text):
        if not is_false_positive(text, m):
            return True
    return False


def readable_feed_name(key):
    for prefix, nice_name in PREFIX_NAMES.items():
        if key.startswith(prefix):
            rest = key[len(prefix):]
            if rest:
                return f"{nice_name} - {rest.replace('_', ' ').title()}"
            return nice_name
    return key.replace("_", " ").title()


def source_name(feed_name):
    """De koepelbron van een feednaam: 'De Tijd - Opinie' -> 'De Tijd'."""
    return feed_name.split(" - ", 1)[0]


def canonical_feeds(feeds):
    """Zet oude feednamen om naar de overgebleven naam en haalt dubbels weg,
    met behoud van de volgorde."""
    uit = []
    for feed in feeds:
        naam = REPLACED_FEEDS.get(feed, feed)
        if naam not in uit:
            uit.append(naam)
    return uit


def write_atomic(path, text):
    """Schrijft eerst naar een tijdelijk bestand en wisselt dat dan om. Zo kan
    een onderbroken run het archief of de pagina nooit half overschrijven."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def parse_date(value):
    """Leest zowel RFC 822 (RSS) als ISO 8601 (Atom) en geeft een naive
    datetime in lokale tijd terug."""
    if not value:
        return None
    value = value.strip()

    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def normalize_link(link):
    """Strip tracking-parameters en een afsluitende slash, zodat hetzelfde
    artikel uit twee feeds ook echt als hetzelfde herkend wordt."""
    if not link:
        return ""
    parts = urlsplit(link.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), "")
    )


def article_key(link, title):
    normalized = normalize_link(link)
    if normalized:
        return normalized
    return "titel::" + re.sub(r"\s+", " ", title.strip().lower())


# --------------------------------------------------------------------------
# Stap 1: feeds ophalen en uitlezen
# --------------------------------------------------------------------------

ATOM_NS = "{http://www.w3.org/2005/Atom}"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class _TagStripper(HTMLParser):
    """Haalt de HTML-tags uit een samenvatting."""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return "".join(self.parts)


def strip_html(text):
    if not text:
        return ""
    stripper = _TagStripper()
    stripper.feed(text)
    return unescape(stripper.get_text()).strip()


def fetch_url(url, timeout=15):
    """Haalt de ruwe inhoud van een URL op. Herpogingen gebeuren een niveau
    hoger, in fetch_source."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def parse_feed(xml_bytes):
    """Zet ruwe XML om naar een lijst artikelen. Ondersteunt RSS 2.0
    (<channel>/<item>) en Atom (<feed>/<entry>)."""
    root = ET.fromstring(xml_bytes)

    if root.tag == f"{ATOM_NS}feed":
        return _parse_atom_entries(root)

    channel = root.find("channel")
    if channel is None:
        raise ValueError("Geen <channel> of Atom <feed> gevonden, dit lijkt geen geldige feed.")
    return _parse_rss_items(channel)


def _parse_rss_items(channel):
    items = []
    for item in channel.findall("item"):
        items.append(
            {
                "title": item.findtext("title", default="").strip(),
                "link": item.findtext("link", default="").strip(),
                "pub_date": item.findtext("pubDate", default="").strip(),
                "description": strip_html(item.findtext("description", default="")),
                "categories": [c.text for c in item.findall("category") if c.text],
            }
        )
    return items


def _parse_atom_entries(root):
    items = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        link = ""
        for l in entry.findall(f"{ATOM_NS}link"):
            if l.get("rel") == "alternate":
                link = l.get("href", "")
                break
        if not link:
            link = entry.findtext(f"{ATOM_NS}id", default="").strip()

        pub_date = entry.findtext(f"{ATOM_NS}published", default="").strip()
        if not pub_date:
            pub_date = entry.findtext(f"{ATOM_NS}updated", default="").strip()

        items.append(
            {
                "title": entry.findtext(f"{ATOM_NS}title", default="").strip(),
                "link": link,
                "pub_date": pub_date,
                "description": strip_html(entry.findtext(f"{ATOM_NS}summary", default="")),
                "categories": [
                    c.get("term") for c in entry.findall(f"{ATOM_NS}category") if c.get("term")
                ],
            }
        )
    return items


def parse_topic_page(html_bytes, limit=10):
    """Haalt de recentste artikelen uit een VRT NWS-onderwerppagina. Die
    pagina's zijn geen feed, maar bevatten de artikellijst als ingebedde JSON
    (Next.js __NEXT_DATA__)."""
    html = html_bytes.decode("utf-8", errors="replace")
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise ValueError("Geen __NEXT_DATA__ gevonden, de opbouw van de pagina is gewijzigd.")

    data = json.loads(match.group(1))
    compositions = data["props"]["pageProps"]["data"]["compositions"][0]["compositions"]

    items = []
    for comp in compositions:
        if comp.get("type") != "articleTeaser":
            continue

        link = ""
        actions = comp.get("actions") or []
        if actions:
            link = actions[0].get("uri", "")

        pub_date = ""
        timestamp = comp.get("publication", {}).get("timestamp")
        if timestamp:
            pub_date = datetime.fromtimestamp(
                timestamp / 1000, tz=timezone.utc
            ).strftime("%a, %d %b %Y %H:%M:%S GMT")

        tag = comp.get("tag", {}).get("text")

        items.append(
            {
                "title": comp.get("title", {}).get("text", "").strip(),
                "link": link,
                "pub_date": pub_date,
                "description": "",
                "categories": [tag] if tag else [],
            }
        )
        if len(items) >= limit:
            break

    return items


class _IntroParser(HTMLParser):
    """Zoekt op een artikelpagina naar een samenvatting: eerst de meta-tags die
    de krant zelf meegeeft voor sociale media, anders de eerste alinea."""

    META_KEYS = ("og:description", "description", "twitter:description")
    SKIP = ("script", "style", "noscript")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metas = {}
        self.first_p = ""
        self._in_p = False
        self._in_skip = 0
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._in_skip += 1
        elif tag == "meta":
            a = dict(attrs)
            sleutel = (a.get("property") or a.get("name") or "").lower()
            inhoud = (a.get("content") or "").strip()
            if sleutel in self.META_KEYS and inhoud and sleutel not in self.metas:
                self.metas[sleutel] = inhoud
        elif tag == "p" and not self.first_p:
            self._in_p, self._buffer = True, []

    def handle_data(self, data):
        if self._in_p and not self._in_skip:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._in_skip:
            self._in_skip -= 1
        elif tag == "p" and self._in_p:
            tekst = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            if len(tekst) >= 60:
                self.first_p = tekst
            self._in_p, self._buffer = False, []

    def get_intro(self):
        for sleutel in self.META_KEYS:
            if self.metas.get(sleutel):
                return self.metas[sleutel]
        return self.first_p


def first_sentences(text, max_len=320):
    """Houdt hele zinnen over tot ongeveer max_len tekens."""
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    text = re.sub(r"^(LIVE|UPDATE)\s+", "", text)
    if len(text) <= max_len:
        return text
    zinnen = re.split(r"(?<=[.!?])\s+", text)
    uit = zinnen[0]
    for zin in zinnen[1:]:
        if len(uit) + 1 + len(zin) > max_len:
            break
        uit += " " + zin
    return uit if len(uit) <= max_len else uit[:max_len].rsplit(" ", 1)[0] + "..."


def fetch_intro(article, retries=2):
    """Haalt de openingszinnen van een artikelpagina. Geeft een lege tekst
    terug als de pagina niets bruikbaars meegeeft, bijvoorbeeld achter een
    betaalmuur."""
    if not article.get("link"):
        return ""
    for attempt in range(retries + 1):
        try:
            html = fetch_url(article["link"], timeout=20).decode("utf-8", errors="replace")
            parser = _IntroParser()
            parser.feed(html)
            return first_sentences(strip_html(parser.get_intro()))
        except OSError:
            if attempt < retries:
                time.sleep(FETCH_BACKOFF * (2 ** attempt))
        except Exception:
            return ""
    return ""


def fill_missing_intros(articles, workers):
    """Vult ontbrekende samenvattingen aan. Elk artikel wordt hoogstens een
    keer geprobeerd, zodat een pagina zonder samenvatting niet elke run
    opnieuw opgehaald wordt."""
    todo = [
        a for a in articles.values()
        if not a["description"] and not a.get("intro_geprobeerd")
    ]
    if not todo:
        return 0

    print(f"Openingszinnen ophalen voor {len(todo)} artikelen zonder samenvatting...")
    gelukt = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for art, intro in zip(todo, pool.map(fetch_intro, todo)):
            art["intro_geprobeerd"] = True
            if intro:
                art["description"] = intro
                gelukt += 1
            else:
                print(f"  ! geen samenvatting gevonden: {art['title'][:70]}")
    return gelukt


def write_items_file(folder, key, url, items):
    """Bewaart de ruwe artikellijst als tekstbestand, in hetzelfde formaat als
    het oude rss_reader.py. Enkel gebruikt bij --archive."""
    lines = [f"Feed: {url}", f"Aantal artikelen gevonden: {len(items)}", ""]
    if not items:
        lines.append("Geen artikelen gevonden in de feed.")
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {item['title']}")
        if item["pub_date"]:
            lines.append(f"   Datum: {item['pub_date']}")
        if item["categories"]:
            lines.append(f"   Categorie: {', '.join(item['categories'])}")
        if item["description"]:
            lines.append(f"   {item['description']}")
        lines.append(f"   Link: {item['link']}")
        lines.append("")
    with open(folder / f"{key}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def collect_sources():
    """Alle bronnen, met hoogstens een ingang per URL. Twee feeds met dezelfde
    URL leveren immers hetzelfde artikel op, wat een verzoek verspilt en op de
    pagina meerdere badges voor eenzelfde bron oplevert."""
    sources = []
    gezien = {}
    for key, url in FEEDS.items():
        if url in gezien:
            print(f"  (overgeslagen: {key} haalt dezelfde URL op als {gezien[url]})")
            continue
        gezien[url] = key
        sources.append({"key": key, "url": url, "kind": "feed", "limit": None})

    for key, (url, limit) in TOPIC_PAGES.items():
        sources.append({"key": key, "url": url, "kind": "page", "limit": limit})
    return sources


def fetch_source(source):
    """Haalt een enkele bron op. Fouten worden niet doorgegooid maar als tekst
    teruggegeven, zodat een kapotte feed de scan niet stopt.

    vrt.be verbreekt de verbinding bij ongeveer vier op de tien verzoeken,
    onmiddellijk en zonder data. Een herpoging lukt meestal wel, dus proberen
    we netwerkfouten meermaals opnieuw met een korte, oplopende wachttijd.
    Parseerfouten hebben geen herpoging nodig: die gaan over de inhoud."""
    last_error = None
    for attempt in range(FETCH_RETRIES + 1):
        try:
            raw = fetch_url(source["url"])
            if source["kind"] == "feed":
                items = parse_feed(raw)
            else:
                items = parse_topic_page(raw, limit=source["limit"])
            return source, items, None, attempt + 1
        except OSError as e:  # URLError, ConnectionReset, timeout
            last_error = f"{type(e).__name__}: {e}"
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_BACKOFF * (2 ** attempt))
        except Exception as e:  # XML, JSON, HTML: opnieuw proberen helpt niet
            return source, [], f"{type(e).__name__}: {e}", attempt + 1
    return source, [], last_error, FETCH_RETRIES + 1


def fetch_all_sources(workers, archive_dir=None):
    sources = collect_sources()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for source, items, error, attempts in pool.map(fetch_source, sources):
            feed_name = readable_feed_name(source["key"])
            extra = f" (na {attempts} pogingen)" if attempts > 1 else ""
            if error:
                print(f"  ! {feed_name}: {error}{extra}", file=sys.stderr)
            else:
                print(f"  - {feed_name}: {len(items)} artikelen{extra}")
                if archive_dir is not None:
                    write_items_file(archive_dir, source["key"], source["url"], items)
            results.append(
                {
                    "name": feed_name,
                    "url": source["url"],
                    "count": len(items),
                    "error": error,
                    "items": items,
                }
            )
    results.sort(key=lambda r: r["name"])
    return results


# --------------------------------------------------------------------------
# Bestaande site inlezen
# --------------------------------------------------------------------------

class ExistingPageParser(HTMLParser):
    """Leest artikelen terug uit een eerder gegenereerde pagina, zodat de
    site ook zonder het JSON-bestand kan verderbouwen. Verwacht de kaart-
    structuur van dit script (article.card met h3 > a) of die van
    build_ai_page.py (div.card met h2 > a), telkens met div.meta,
    span.badge en p.desc."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.articles = []
        self._card = None
        self._card_tag = None
        self._depth = 0
        self._collect = None
        self._buffer = []

    @staticmethod
    def _has_class(attrs, name):
        return name in dict(attrs).get("class", "").split()

    def handle_starttag(self, tag, attrs):
        if self._card is None:
            if tag in ("div", "article") and self._has_class(attrs, "card"):
                self._card = {"title": "", "link": "", "date": "", "description": "",
                              "feeds": [], "first_seen": ""}
                self._card_tag = tag
                self._depth = 1
            return

        if tag == self._card_tag:
            self._depth += 1

        if tag == "a" and not self._card["link"]:
            self._card["link"] = dict(attrs).get("href", "")
            self._collect, self._buffer = "title", []
        elif tag == "div" and self._has_class(attrs, "meta"):
            self._collect, self._buffer = "date", []
        elif tag == "span" and self._has_class(attrs, "badge"):
            self._collect, self._buffer = "badge", []
        elif tag == "p" and self._has_class(attrs, "desc"):
            self._collect, self._buffer = "description", []

    def handle_data(self, data):
        if self._collect:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        if self._card is None:
            return

        if self._collect:
            text = unescape("".join(self._buffer)).strip()
            if self._collect == "badge":
                if text and text.upper() != "NIEUW" and text not in self._card["feeds"]:
                    self._card["feeds"].append(text)
            else:
                self._card[self._collect] = text
            self._collect, self._buffer = None, []

        if tag == self._card_tag:
            self._depth -= 1
            if self._depth == 0:
                if self._card["title"]:
                    self.articles.append(self._card)
                self._card, self._card_tag = None, None


def load_existing(data_path, html_path):
    """Bestaande artikelen: eerst uit het JSON-bestand, anders gereconstrueerd
    uit de HTML-pagina."""
    data = None
    if data_path.exists():
        rauw = data_path.read_text(encoding="utf-8")
        try:
            data = json.loads(rauw)
        except json.JSONDecodeError as e:
            # Gebeurt als git het archief met conflictmarkeringen heeft
            # samengevoegd: de bot commit dit bestand en jij ook. Doorgaan met
            # de pagina is beter dan de hele run laten mislukken.
            reden = ("het bevat merge-conflictmarkeringen"
                     if "<<<<<<<" in rauw else str(e))
            print(f"! {data_path} is onleesbaar ({reden}).", file=sys.stderr)
            print("  Het archief wordt hersteld uit de bestaande pagina. Artikelen "
                  "die niet meer op de pagina staan, gaan daarbij verloren.",
                  file=sys.stderr)

    if data is not None:
        articles = {}
        for art in data.get("articles", []):
            dt = parse_date(art.get("date"))
            if dt is None:
                continue
            articles[article_key(art.get("link", ""), art.get("title", ""))] = {
                "title": art.get("title", ""),
                "link": art.get("link", ""),
                "description": art.get("description", ""),
                "date": dt,
                "feeds": canonical_feeds(art.get("feeds", [])),
                "first_seen": art.get("first_seen", ""),
                "intro_geprobeerd": art.get("intro_geprobeerd", False),
            }
        feeds = [
            fo for fo in data.get("feeds", [])
            if fo.get("name") not in REPLACED_FEEDS
        ]
        return articles, feeds, "json"

    if html_path.exists():
        parser = ExistingPageParser()
        with open(html_path, encoding="utf-8") as f:
            parser.feed(f.read())
        articles = {}
        for art in parser.articles:
            try:
                dt = datetime.strptime(art["date"], "%d/%m/%Y %H:%M")
            except ValueError:
                continue
            articles[article_key(art["link"], art["title"])] = {
                "title": art["title"],
                "link": art["link"],
                "description": art["description"],
                "date": dt,
                "feeds": canonical_feeds(art["feeds"]),
                "first_seen": "",
                "intro_geprobeerd": bool(art["description"]),
            }
        return articles, [], "html"

    return {}, [], "leeg"


def save_data(data_path, articles, feeds_overview):
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "feeds": feeds_overview,
        "articles": [
            {
                "title": a["title"],
                "link": a["link"],
                "description": a["description"],
                "date": a["date"].isoformat(timespec="seconds"),
                "feeds": a["feeds"],
                "first_seen": a["first_seen"],
                "intro_geprobeerd": a.get("intro_geprobeerd", False),
            }
            for a in sorted(articles.values(), key=lambda a: a["date"], reverse=True)
        ],
    }
    write_atomic(data_path, json.dumps(payload, ensure_ascii=False, indent=1))


# --------------------------------------------------------------------------
# Stap 2: filteren en samenvoegen
# --------------------------------------------------------------------------

def merge_new_articles(existing, feed_results, days):
    """Voegt de AI-artikelen uit de opgehaalde feeds toe aan wat er al is.
    Geeft de lijst met echt nieuwe artikelen terug."""
    cutoff = TODAY - timedelta(days=days)
    horizon = TODAY + timedelta(days=1)
    today_iso = TODAY.isoformat()
    added = []

    for result in feed_results:
        feed_name = result["name"]
        for item in result["items"]:
            dt = parse_date(item.get("pub_date"))
            if dt is None or not (cutoff <= dt.date() <= horizon):
                continue

            haystack = f"{item.get('title', '')} {item.get('description', '')}"
            if not has_real_ai_match(haystack):
                continue

            key = article_key(item.get("link", ""), item.get("title", ""))
            if not key or key == "titel::":
                continue

            if key in existing:
                article = existing[key]
                if feed_name not in article["feeds"]:
                    article["feeds"].append(feed_name)
                if not article["description"] and item.get("description"):
                    article["description"] = item["description"]
                continue

            article = {
                "title": item.get("title", "").strip(),
                "link": item.get("link", "").strip(),
                "description": item.get("description", "").strip(),
                "date": dt,
                "feeds": [feed_name],
                "first_seen": today_iso,
                "intro_geprobeerd": False,
            }
            existing[key] = article
            added.append(article)

    return added


# --------------------------------------------------------------------------
# Stap 3: de website bouwen
# --------------------------------------------------------------------------

CSS = """
:root {
    --paars: #5B3A8C;
    --diep: #3A2459;
    --zacht: #9B7DC4;
    --cream: #F5EFE3;
    --inkt: #1F1A2E;
}
* { box-sizing: border-box; }
body {
    background-color: var(--cream);
    color: var(--inkt);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Arial, sans-serif;
    margin: 0;
    padding: 0 20px 60px;
    line-height: 1.55;
}
header {
    max-width: 900px;
    margin: 0 auto;
    padding: 44px 0 20px;
    text-align: center;
    border-bottom: 2px solid var(--zacht);
}
header h1 { color: var(--paars); margin: 0 0 8px; font-size: 2rem; }
header .subtitle { color: var(--diep); font-size: 1rem; opacity: 0.85; }
main { max-width: 900px; margin: 0 auto; }
.filters {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin: 24px 0 8px;
}
.filters input, .filters select {
    flex: 1 1 220px;
    padding: 9px 12px;
    border: 1px solid var(--zacht);
    border-radius: 8px;
    background: #ffffff;
    color: var(--diep);
    font-size: 0.95rem;
    font-family: inherit;
}
.filters input:focus, .filters select:focus {
    outline: 2px solid var(--paars);
    outline-offset: 1px;
}
#teller { font-size: 0.85rem; color: var(--diep); opacity: 0.7; margin-bottom: 8px; }
.daggroep { margin-top: 28px; }
.daggroep > h2 {
    color: var(--paars);
    font-size: 1rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin: 0 0 4px;
    padding-bottom: 6px;
    border-bottom: 1px solid var(--zacht);
}
.card {
    background: #ffffff;
    border: 1px solid var(--zacht);
    border-radius: 10px;
    padding: 18px 22px;
    margin: 16px 0;
    box-shadow: 0 2px 6px rgba(91, 58, 140, 0.08);
}
.card h3 { margin: 0 0 8px; font-size: 1.15rem; line-height: 1.35; }
.card h3 a { color: var(--paars); text-decoration: none; }
.card h3 a:hover { text-decoration: underline; }
.meta { color: var(--diep); font-size: 0.85rem; opacity: 0.7; margin-bottom: 10px; }
.badges { margin-bottom: 10px; }
.badge {
    display: inline-block;
    background: var(--zacht);
    color: #ffffff;
    border-radius: 12px;
    padding: 3px 12px;
    font-size: 0.75rem;
    margin: 0 6px 4px 0;
}
.badge.nieuw { background: var(--paars); letter-spacing: 0.06em; }
.desc { color: var(--diep); font-size: 0.95rem; margin: 0; }
.leeg { text-align: center; margin-top: 40px; color: var(--diep); opacity: 0.7; }
section.overview { margin-top: 54px; }
section.overview h2 {
    color: var(--paars);
    border-bottom: 2px solid var(--zacht);
    padding-bottom: 8px;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 16px;
    background: #ffffff;
    border-radius: 8px;
    overflow: hidden;
}
th, td {
    text-align: left;
    padding: 10px 14px;
    border-bottom: 1px solid #e5ddc8;
    font-size: 0.9rem;
    word-break: break-word;
}
th { background: var(--paars); color: #ffffff; }
td a { color: var(--paars); }
td.fout { color: #7a2f2f; }
footer {
    max-width: 900px;
    margin: 40px auto 0;
    text-align: center;
    font-size: 0.8rem;
    color: var(--diep);
    opacity: 0.6;
}
@media (max-width: 600px) {
    header h1 { font-size: 1.5rem; }
    .card { padding: 16px; }
}
"""

JS = """
(function () {
    var zoek = document.getElementById('zoek');
    var bron = document.getElementById('bron');
    var teller = document.getElementById('teller');
    var kaarten = Array.prototype.slice.call(document.querySelectorAll('.card'));
    var groepen = Array.prototype.slice.call(document.querySelectorAll('.daggroep'));

    function filter() {
        var term = zoek.value.trim().toLowerCase();
        var gekozen = bron.value;
        var zichtbaar = 0;
        kaarten.forEach(function (kaart) {
            var tekstOk = !term || kaart.dataset.tekst.indexOf(term) !== -1;
            var bronOk = !gekozen || kaart.dataset.bronnen.indexOf('|' + gekozen + '|') !== -1;
            var toon = tekstOk && bronOk;
            kaart.hidden = !toon;
            if (toon) { zichtbaar++; }
        });
        groepen.forEach(function (groep) {
            var open = groep.querySelectorAll('.card:not([hidden])').length;
            groep.hidden = open === 0;
        });
        teller.textContent = zichtbaar + ' van ' + kaarten.length + ' artikelen zichtbaar';
    }

    zoek.addEventListener('input', filter);
    bron.addEventListener('change', filter);
    filter();
})();
"""

MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
           "augustus", "september", "oktober", "november", "december"]
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def dutch_date(d):
    return f"{DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]} {d.year}"


def build_html(articles, feeds_overview, new_keys, page_days=PAGE_DAYS):
    """Bouwt de pagina met de artikelen van de voorbije page_days dagen. Het
    archief houdt alles bij, de pagina toont enkel het recente venster."""
    alles = sorted(articles.items(), key=lambda kv: kv[1]["date"], reverse=True)
    vensterstart = TODAY - timedelta(days=page_days)
    sorted_articles = [kv for kv in alles if kv[1]["date"].date() >= vensterstart]

    groups = []
    for key, art in sorted_articles:
        day = art["date"].date()
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append((key, art))

    sources = sorted({source_name(f) for _, art in sorted_articles for f in art["feeds"]})
    options = "".join(
        f'<option value="{escape(s)}">{escape(s)}</option>' for s in sources
    )

    groups_html = []
    for day, items in groups:
        cards = []
        for key, art in items:
            badges = "".join(
                f'<span class="badge">{escape(feed)}</span>' for feed in art["feeds"]
            )
            if key in new_keys:
                badges = '<span class="badge nieuw">NIEUW</span>' + badges
            zoektekst = escape(
                f"{art['title']} {art['description']} {' '.join(art['feeds'])}".lower(),
                quote=True,
            )
            bronnen = escape(
                "|" + "|".join(sorted({source_name(f) for f in art["feeds"]} |
                                      set(art["feeds"]))) + "|",
                quote=True,
            )
            link = escape(art["link"]) if art["link"] else "#"
            cards.append(f"""
            <article class="card" data-tekst="{zoektekst}" data-bronnen="{bronnen}">
                <h3><a href="{link}" target="_blank" rel="noopener noreferrer">{escape(art['title'])}</a></h3>
                <div class="meta">{art['date'].strftime('%d/%m/%Y %H:%M')}</div>
                <div class="badges">{badges}</div>
                <p class="desc">{escape(art['description'])}</p>
            </article>""")
        groups_html.append(
            f'\n        <section class="daggroep">\n'
            f'            <h2>{escape(dutch_date(day))}</h2>'
            + "".join(cards)
            + "\n        </section>"
        )

    if sorted_articles:
        oldest = sorted_articles[-1][1]["date"].date()
        newest = sorted_articles[0][1]["date"].date()
        range_text = f"{oldest.strftime('%d/%m/%Y')} tot {newest.strftime('%d/%m/%Y')}"
    else:
        range_text = "nog geen artikelen"

    ouder = len(alles) - len(sorted_articles)
    archief_tekst = f" &middot; {ouder} oudere in het archief" if ouder else ""

    if page_days == 31:
        venster_tekst = "van de voorbije maand"
    elif page_days == 7:
        venster_tekst = "van de voorbije week"
    else:
        venster_tekst = f"van de voorbije {page_days} dagen"

    # De tabel telt enkel de AI-artikelen die uit een feed kwamen, niet alles
    # wat die feed publiceert.
    ai_per_feed = {}
    for _, art in sorted_articles:
        for feed in art["feeds"]:
            ai_per_feed[feed] = ai_per_feed.get(feed, 0) + 1

    # De productiefste feeds bovenaan. Feeds zonder treffers volgen alfabetisch,
    # feeds die niet opgehaald raakten staan onderaan.
    gesorteerd = sorted(
        feeds_overview,
        key=lambda fo: (
            -ai_per_feed.get(fo["name"], 0),
            bool(fo.get("error")),
            fo["name"],
        ),
    )

    feed_rows = "".join(
        f"""<tr>
                    <td>{escape(fo['name'])}</td>
                    <td><a href="{escape(fo['url'])}" target="_blank" rel="noopener noreferrer">{escape(fo['url'])}</a></td>
                    <td{' class="fout"' if fo.get('error') else ''}>{escape(fo['error']) if fo.get('error') else ai_per_feed.get(fo['name'], 0)}</td>
                </tr>"""
        for fo in gesorteerd
    )

    body = "".join(groups_html) if groups_html else (
        '<p class="leeg">Nog geen AI-gerelateerde artikelen gevonden.</p>'
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="nl">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        "<title>AI in het nieuws</title>\n"
        "<style>" + CSS + "</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        "    <h1>AI in het nieuws</h1>\n"
        f'    <div class="subtitle">{len(sorted_articles)} artikelen uit de Nederlandstalige pers '
        f"{venster_tekst} &middot; {escape(range_text)}{archief_tekst}</div>\n"
        f'    <div class="subtitle">Laatst bijgewerkt op {dutch_date(TODAY)} '
        f"&middot; {len(new_keys)} nieuw</div>\n"
        "</header>\n<main>\n"
        '    <div class="filters">\n'
        '        <input id="zoek" type="search" placeholder="Zoek in titels en samenvattingen">\n'
        '        <select id="bron"><option value="">Alle bronnen</option>' + options + "</select>\n"
        "    </div>\n"
        '    <div id="teller"></div>\n'
        + body
        + "\n\n    <section class=\"overview\">\n"
        "        <h2>Overzicht van alle gescande feeds</h2>\n"
        "        <table>\n"
        "            <thead>\n"
        "                <tr><th>Feed</th><th>URL</th><th>AI-artikelen</th></tr>\n"
        "            </thead>\n"
        "            <tbody>\n                " + feed_rows + "\n            </tbody>\n"
        "        </table>\n"
        "    </section>\n"
        "</main>\n"
        f'<footer>Gegenereerd op {TODAY.strftime("%d/%m/%Y")} &middot; Het masker van AI</footer>\n'
        "<script>" + JS + "</script>\n"
        "</body>\n</html>\n"
    )


# --------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(
        description="Scant de nieuwsfeeds, filtert AI-artikelen en bouwt of vult de website aan."
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Hoe ver terug nieuwe artikelen mogen dateren (standaard {DEFAULT_DAYS})")
    parser.add_argument("--out", default=None,
                        help=f"Pad van de HTML-pagina (standaard {DEFAULT_OUT} naast het script)")
    parser.add_argument("--data", default=None,
                        help=f"Pad van het JSON-archief (standaard {DEFAULT_DATA} naast het script)")
    parser.add_argument("--page-days", type=int, default=PAGE_DAYS,
                        help=f"Hoeveel dagen de pagina toont (standaard {PAGE_DAYS}); "
                             "het archief bewaart alles")
    parser.add_argument("--no-intro", action="store_true",
                        help="Haal geen openingszinnen op voor artikelen zonder samenvatting")
    parser.add_argument("--archive", action="store_true",
                        help="Bewaar ook de ruwe feeds als .txt in een map met de datum van vandaag")
    parser.add_argument("--workers", type=int, default=8,
                        help="Aantal feeds dat tegelijk opgehaald wordt (standaard 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Toon wat er zou bijkomen, zonder iets weg te schrijven")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else SCRIPT_DIR / DEFAULT_OUT
    data_path = Path(args.data) if args.data else SCRIPT_DIR / DEFAULT_DATA

    existing, old_feeds, bron = load_existing(data_path, out_path)
    if bron == "json":
        print(f"Bestaand archief: {len(existing)} artikelen uit {data_path}")
    elif bron == "html":
        print(f"Bestaande pagina gevonden: {len(existing)} artikelen hersteld uit {out_path}")
    else:
        print("Nog geen bestaande site, we beginnen van nul.")

    archive_dir = None
    if args.archive and not args.dry_run:
        archive_dir = SCRIPT_DIR / TODAY.isoformat()
        archive_dir.mkdir(parents=True, exist_ok=True)

    print("Feeds ophalen...")
    feed_results = fetch_all_sources(args.workers, archive_dir)

    added = merge_new_articles(existing, feed_results, args.days)
    new_keys = {article_key(a["link"], a["title"]) for a in added}

    feeds_overview = [
        {"name": r["name"], "url": r["url"], "count": r["count"], "error": r["error"]}
        for r in feed_results
    ]
    if not feeds_overview:
        feeds_overview = old_feeds

    print(f"\nNieuwe AI-artikelen: {len(added)}")
    for art in sorted(added, key=lambda a: a["date"], reverse=True):
        print(f"  + [{art['date'].strftime('%d/%m %H:%M')}] {art['title']} ({art['feeds'][0]})")

    if args.dry_run:
        print("\n--dry-run: er is niets weggeschreven.")
        return

    if not args.no_intro:
        aangevuld = fill_missing_intros(existing, args.workers)
        if aangevuld:
            print(f"Samenvattingen aangevuld: {aangevuld}")

    html = build_html(existing, feeds_overview, new_keys, args.page_days)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out_path, html)
    save_data(data_path, existing, feeds_overview)

    op_pagina = sum(
        1 for a in existing.values()
        if a["date"].date() >= TODAY - timedelta(days=args.page_days)
    )
    print(f"\nWebsite bijgewerkt: {out_path} ({op_pagina} artikelen op de pagina, "
          f"{len(existing)} in het archief)")
    print(f"Archief bijgewerkt: {data_path}")

    problems = [f["name"] for f in feeds_overview if f.get("error")]
    empty = [f["name"] for f in feeds_overview if not f.get("error") and f["count"] == 0]
    if problems:
        print(f"Feeds met een fout: {', '.join(problems)}")
    if empty:
        print(f"Feeds met 0 artikelen: {', '.join(empty)}")


if __name__ == "__main__":
    main()
