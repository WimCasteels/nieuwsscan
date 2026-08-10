# AI-nieuwsscan

Scant elke nacht de nieuwsfeeds van de Vlaamse pers, houdt de AI-gerelateerde
artikelen over en bouwt daarmee een overzichtspagina. Onderdeel van
[Het masker van AI](https://www.lannoocampus.be/nl/het-masker-van-ai).

De site staat in `docs/` en wordt gepubliceerd via GitHub Pages.

## Hoe het werkt

Eén script, `ai_nieuwsscan.py`, doet alles en gebruikt enkel de Python
standaardbibliotheek. Geen installatie nodig.

```
python ai_nieuwsscan.py
```

De drie stappen:

1. **Ophalen.** 34 bronnen: RSS- en Atom-feeds van De Standaard, De Tijd,
   De Morgen, VRT NWS, Knack, Data News, Apache.be en Humo, plus de AI-rubriek
   van VRT NWS die geen feed heeft en uit de pagina zelf gelezen wordt. De
   feeds staan in `FEEDS` en `TOPIC_PAGES` bovenaan het script.
2. **Filteren.** Artikelen van de voorbije zeven dagen die een AI-trefwoord
   bevatten in titel of samenvatting. De trefwoorden staan gegroepeerd in
   `AI_KEYWORDS`: algemene begrippen, namen van bedrijven en modellen,
   infrastructuur zoals chips en datacenters, en toepassingen.
3. **Bouwen.** De pagina toont de voorbije 31 dagen, gegroepeerd per dag, met
   een zoekveld en een bronfilter. Nieuwe artikelen komen erbij, bestaande
   blijven staan.

`ai_artikelen.json` is het archief en bewaart alles, ook wat van de pagina
verdwenen is. Verlies je dat bestand, dan leest het script de bestaande pagina
terug in en gaat het gewoon verder.

## Opties

| Optie | Wat het doet |
|---|---|
| `--days 14` | Hoe ver terug nieuwe artikelen mogen dateren (standaard 7) |
| `--page-days 7` | Hoeveel dagen de pagina toont (standaard 31) |
| `--out pad.html` | Ander pad voor de pagina (standaard `docs/index.html`) |
| `--data pad.json` | Ander pad voor het archief |
| `--dry-run` | Toon wat erbij zou komen, schrijf niets weg |
| `--no-intro` | Haal geen openingszinnen op bij artikelen zonder samenvatting |
| `--archive` | Bewaar ook de ruwe feeds als tekstbestand per dag |
| `--workers 8` | Aantal feeds dat tegelijk opgehaald wordt |

## Automatisch

`.github/workflows/nieuwsscan.yml` draait de scan elke nacht, commit het
resultaat en publiceert de pagina. De workflow zet `TZ` op `Europe/Brussels`,
want zonder dat staat de runner op UTC en schuiven alle artikeltijden twee uur
op.

Je kan hem ook met de hand starten via het tabblad Actions.

## Aandachtspunten

- Vrt.be verbreekt bij ongeveer vier op de tien verzoeken meteen de
  verbinding. Het script probeert daarom tot vijf keer met een oplopende
  wachttijd. Andere sites doen dit niet.
- De artikelpagina's van De Morgen geven geen samenvatting mee, dus die kaarten
  blijven kaal. Elk artikel wordt daarvoor hoogstens een keer opgehaald.
- Een feed die van URL verandert, geeft geen fout maar levert stilletjes nul
  artikelen. De tabel onderaan de pagina toont dat.
