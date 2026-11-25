#!/usr/bin/env python3
"""Hold øje med tilbud i tilbudsaviserne (eTilbudsavis/Tjek) og sammenlign på enhedspris.

Drikkevarer sammenlignes på kr pr. liter ("enhed": "l", standard), fx smør på kr pr. kg ("enhed": "kg").

Hvilke varer der holdes øje med, står i config.json under "varer".

Skriver:
  OVERSIGT.md        – overblik pr. vare, sorteret efter enhedspris
  tilbud.ics         – kalender, der kan abonneres på
  data/tilbud.json   – alle relevante tilbud, der er set (historik)
  data/nye.md        – kun når der er nye tilbud under alarmgrænsen (bruges til alarmen,
  data/nye_titel.txt   og committes ikke)

Kun standardbiblioteket bruges, så scriptet kan køre direkte i GitHub Actions.
"""

import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MAPPE = Path(__file__).resolve().parent
DATA = MAPPE / "data"
TZ = ZoneInfo("Europe/Copenhagen")
API = "https://squid-api.tjek.com/v2/offers/search"
AVIS_LINK = "https://etilbudsavis.dk/"

UGEDAGE = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]

TAL = r"\d+(?:[.,]\d+)?"
# "24x33 cl", "24 x 0,33 l", "24 X 33 CL"
PAKKE_RE = re.compile(rf"(\d{{1,2}})\s*[x×]\s*({TAL})\s*(cl|l|liter)\b", re.I)
# "1,5 liter", "50 cl", "50-150 cl", "0,5-1,5 liter"
STR_RE = re.compile(rf"(?<![\d,.x×])({TAL})(?:\s*-\s*({TAL}))?\s*(cl|l|liter)\b", re.I)
DAASE_RE = re.compile(r"\b(ds|dåse|dåser)\b", re.I)
FLASKE_RE = re.compile(r"\b(pet|fl|flaske|flasker|plast)\b", re.I)
GRAM_RE = re.compile(rf"(?<![\d,.])({TAL})\s*(g|gr|gram|kg)\b\.?", re.I)
MAX_RE = re.compile(r"ma(?:x|ks)\.?\s*(\d+)\s*(rammer|ramme|pakker|pakke|ks|kasser|stk|flasker)\.?",
                    re.I)


# ---------------------------------------------------------------- hent data

def hent(config):
    pos = config["position"]
    soegeord = {o for v in config["varer"] for o in v["soegeord"]}
    alle = {}
    for ord_ in sorted(soegeord):
        offset = 0
        while True:
            params = {"query": ord_, "r_lat": pos["lat"], "r_lng": pos["lng"],
                      "r_radius": pos["radius_meter"], "limit": 100, "offset": offset}
            req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params),
                                         headers={"Accept": "application/json",
                                                  "User-Agent": "tilbudsjaeger/1.0"})
            with urllib.request.urlopen(req, timeout=30) as svar:
                side = json.load(svar)
            for o in side:
                alle[o["id"]] = o
            if len(side) < 100:
                break
            offset += 100
    return list(alle.values())


# ---------------------------------------------------------------- fortolkning

def til_cl(tal, enhed):
    v = float(tal.replace(",", "."))
    return round(v * 100) if enhed.lower() in ("l", "liter") else round(v)


def lokal_dato(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").astimezone(TZ).date()


def er_graense(o, config):
    dealer = o.get("dealer") or {}
    butik = (o.get("branding") or {}).get("name") or dealer.get("name") or ""
    if butik in config["udeluk_butikker"]:
        return True
    markeder = [m.get("country_code") for m in dealer.get("markets") or []]
    return config["udeluk_graensehandel"] and "DE" in markeder


def naevner(lav, udtryk, ord_hver_for_sig=False):
    """Står udtrykket i teksten? Med ord_hver_for_sig skal hvert ord bare stå et sted som helt ord."""
    if not ord_hver_for_sig:
        return udtryk in lav
    return all(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", lav) for o in udtryk.split())


def match_vare(lav, vare):
    """'ja' hvis varen nævnes direkte, 'måske' ved fx 'Pepsi … flere varianter' eller kun mærket
    ('ogsaa_uden_variant'), ellers None."""
    hver = vare.get("ord_hver_for_sig", False)
    if any(naevner(lav, o, hver) for o in vare["skal_indeholde"]):
        return "ja"
    if "flere varianter" in lav and any(o in lav for o in vare.get("ogsaa_flere_varianter_af", [])):
        return "måske"
    if any(o in lav for o in vare.get("ogsaa_uden_variant", [])):
        return "måske"
    return None


def fortolk(o, vare):
    """Returnér tilbuddet som dict for denne vare, eller None hvis det ikke handler om varen."""
    overskrift = (o.get("heading") or "").strip()
    beskrivelse = " ".join((o.get("description") or "").split())
    tekst = f"{overskrift} {beskrivelse}"
    lav = tekst.lower()
    status = match_vare(lav, vare)
    if not status:
        return None
    if vare.get("enhed", "l") == "kg":
        return fortolk_vaegt(o, vare, overskrift, beskrivelse, tekst, lav, status)
    noegler = [n.lower() for n in vare["skal_indeholde"] + vare.get("ogsaa_flere_varianter_af", [])]

    # Et tilbud kan dække flere varer ("18x33 cl Heineken … 24x33 cl Pepsi Max").
    # Del beskrivelsen op ved hver pakning og brug den del, der nævner varen.
    match = list(PAKKE_RE.finditer(beskrivelse))
    antal = None
    stoerrelser = []
    afsnit = tekst
    if match:
        dele = [(m, beskrivelse[m.start():(match[i + 1].start() if i + 1 < len(match)
                                            else len(beskrivelse))])
                for i, m in enumerate(match)]
        valgt = next((d for d in dele if any(n in d[1].lower() for n in noegler)), None)
        if valgt is None and len({(m.group(1), til_cl(m.group(2), m.group(3)))
                                  for m, _ in dele}) == 1:
            valgt = dele[0]
        if valgt:
            m, afsnit = valgt
            antal = int(m.group(1))
            stoerrelser = [til_cl(m.group(2), m.group(3))]
            afsnit += " " + overskrift

    q = o.get("quantity") or {}
    api_cl = []
    s = q.get("size") or {}
    si = (q.get("unit") or {}).get("si") or {}
    if si.get("symbol") == "l" and s.get("from"):
        api_cl = sorted({round(s["from"] * si["factor"] * 100), round(s["to"] * si["factor"] * 100)})
    stk = q.get("pieces") or {}

    # Nogle aviser angiver kun den samlede mængde (fx 792 cl = 24 × 33 cl)
    if (not stoerrelser and api_cl and stk.get("to") == 1 and max(api_cl) > vare["max_cl"]
            and all(c % 33 == 0 and c // 33 >= 4 for c in api_cl)):
        antal = max(api_cl) // 33
        stoerrelser = [33]
        afsnit = tekst
        if len(api_cl) > 1:
            noter_pakning = f"flere pakninger: {api_cl[0] // 33}–{antal} stk"
        else:
            noter_pakning = "antal udregnet fra samlet mængde"
    else:
        noter_pakning = None

    if not stoerrelser:
        m = STR_RE.search(beskrivelse)
        if m:
            stoerrelser = [til_cl(m.group(1), m.group(3))]
            if m.group(2):
                stoerrelser.append(til_cl(m.group(2), m.group(3)))
        else:
            stoerrelser = api_cl
    if antal is None:
        antal = int(stk["from"]) if stk.get("from") and stk.get("from") == stk.get("to") else 1

    if FLASKE_RE.search(afsnit):
        type_ = "flaske"
    elif DAASE_RE.search(afsnit):
        type_ = "dåse"
    else:
        type_ = "?"

    noter = [noter_pakning] if noter_pakning else []
    noter += faelles_noter(o, tekst, lav)
    if len(stoerrelser) > 1:
        noter.append("flere størrelser: " + "–".join(f"{s} cl" for s in stoerrelser))
    if status == "måske":
        noter.append(usikker_note(lav, vare))

    return {
        "id": o["id"],
        "vare": vare["navn"],
        "butik": (o.get("branding") or {}).get("name") or (o.get("dealer") or {}).get("name") or "?",
        "overskrift": overskrift,
        "beskrivelse": beskrivelse,
        "pris": float(o["pricing"]["price"]),
        "antal": antal,
        "stoerrelser_cl": stoerrelser,
        "type": type_,
        "status": status,
        "fra": lokal_dato(o["run_from"]).isoformat(),
        "til": lokal_dato(o["run_till"]).isoformat(),
        "noter": noter,
    }


def faelles_noter(o, tekst, lav):
    noter = []
    m = MAX_RE.search(tekst)
    if m:
        noter.append(f"max {m.group(1)} {m.group(2).lower()}")
    elif ((o.get("quantity") or {}).get("pieces") or {}).get("max"):
        noter.append(f"max {o['quantity']['pieces']['max']} stk")
    if "afhentningspris" in lav:
        noter.append("afhentningspris")
    if "medlem" in lav:
        noter.append("medlemspris")
    return noter


def usikker_note(lav, vare):
    if "flere varianter" in lav:
        return f"„flere varianter“, {vare['navn']} ikke nævnt"
    return f"{vare['navn']} ikke nævnt"


def fortolk_vaegt(o, vare, overskrift, beskrivelse, tekst, lav, status):
    """Varer, der sælges efter vægt (fx smør): find gram pr. pakning."""
    q = o.get("quantity") or {}
    s = q.get("size") or {}
    si = (q.get("unit") or {}).get("si") or {}
    gram = []
    if si.get("symbol") == "kg" and s.get("from"):
        gram = sorted({round(s["from"] * si["factor"] * 1000), round(s["to"] * si["factor"] * 1000)})
    if not gram:
        m = GRAM_RE.search(beskrivelse) or GRAM_RE.search(overskrift)
        if m:
            v = float(m.group(1).replace(",", "."))
            gram = [round(v * 1000) if m.group(2).lower() == "kg" else round(v)]
    stk = q.get("pieces") or {}
    antal = int(stk["from"]) if stk.get("from") and stk.get("from") == stk.get("to") else 1
    noter = faelles_noter(o, tekst, lav)
    if len(gram) > 1:
        noter.append("flere størrelser: " + "–".join(f"{g} g" for g in gram))
    if status == "måske":
        noter.append(usikker_note(lav, vare))
    return {
        "id": o["id"], "vare": vare["navn"],
        "butik": (o.get("branding") or {}).get("name") or (o.get("dealer") or {}).get("name") or "?",
        "overskrift": overskrift, "beskrivelse": beskrivelse,
        "pris": float(o["pricing"]["price"]), "antal": antal, "stoerrelser_g": gram, "type": "?",
        "status": status,
        "fra": lokal_dato(o["run_from"]).isoformat(), "til": lokal_dato(o["run_till"]).isoformat(),
        "noter": noter,
    }


def vurder(t, vare):
    """Vælg størrelse inden for grænserne og regn priser ud. False hvis tilbuddet ikke tæller med."""
    t["stykpris"] = round(t["pris"] / t["antal"], 2)
    if vare.get("enhed", "l") == "kg":
        tilladte = [g for g in t["stoerrelser_g"] if vare.get("min_g", 0) <= g <= vare.get("max_g", 10**6)]
        if not tilladte:
            return False
        g = max(tilladte)
        t.update(gram=g, maengde=g, enhed="kg",
                 kilopris=round(t["pris"] / (t["antal"] * g / 1000), 2))
        t["enhedspris"] = t["kilopris"]
        return True
    tilladte = [s for s in t["stoerrelser_cl"] if vare["min_cl"] <= s <= vare["max_cl"]]
    if not tilladte:
        return False
    cl = max(tilladte)  # prisen gælder hver størrelse; den største giver bedst literpris
    t["cl"] = cl
    t["literpris"] = round(t["pris"] / (t["antal"] * cl / 100), 2)
    t.update(maengde=cl, enhed="l", enhedspris=t["literpris"])
    return True


def normaliser(t):
    """Ældre historik kendte kun liter."""
    if "enhedspris" not in t and "literpris" in t:
        t.update(maengde=t["cl"], enhed="l", enhedspris=t["literpris"])
    return t


def under_graense(t, vare, felt="alarm"):
    """Er tilbuddet under varens grænse? felt = 'alarm' eller 'kalender'. None hvis ingen grænse."""
    pr_pakke = vare.get(f"{felt}_max_pris")
    pr_enhed = vare.get(f"{felt}_max_literpris") or vare.get(f"{felt}_max_enhedspris")
    if pr_pakke is not None:
        return t["stykpris"] <= pr_pakke
    if pr_enhed is not None:
        return t["enhedspris"] <= pr_enhed
    return None


def graense_tekst(vare, felt="alarm"):
    if vare.get(f"{felt}_max_pris") is not None:
        return f"højst {kr(vare[f'{felt}_max_pris'])} kr pr. pakke"
    g = vare.get(f"{felt}_max_literpris") or vare.get(f"{felt}_max_enhedspris")
    return f"højst {kr(g)} kr/{vare.get('enhed', 'l')}" if g is not None else ""


# ---------------------------------------------------------------- formattering

def noegle(t):
    """Samme tilbud kan have flere ID'er (fx regionale udgaver af avisen); kend det på indholdet."""
    return f"{t['vare']}|{t['butik']}|{t['fra']}|{t['til']}|{t['pris']}|{t['antal']}|{t.get('cl', t.get('gram'))}"


def kr(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def kort_dato(iso):
    d = date.fromisoformat(iso)
    return f"{UGEDAGE[d.weekday()]} {d.day}/{d.month}"


def periode(t):
    return f"{kort_dato(t['fra'])} – {kort_dato(t['til'])}"


def liter(cl):
    return f"{cl} cl" if cl < 100 else f"{kr(cl / 100).rstrip('0').rstrip(',')} l"


def maengde_tekst(t):
    return f"{t['gram']} g" if t.get("enhed") == "kg" else liter(t["cl"])


def pakning(t):
    return maengde_tekst(t) if t["antal"] == 1 else f"{t['antal']} × {maengde_tekst(t)}"


def tabel(tilbud):
    e = tilbud[0]["enhed"] if tilbud else "l"
    linjer = [f"| Kr/{e} | Kr/stk | Butik | Pris | Pakning | Type | Periode | Noter |",
              "|---:|---:|---|---:|---|:-:|---|---|"]
    for t in tilbud:
        linjer.append(f"| **{kr(t['enhedspris'])}** | {kr(t['stykpris'])} | {t['butik']} "
                      f"| {kr(t['pris'])} | {pakning(t)} | {t['type']} | {periode(t)} "
                      f"| {', '.join(t['noter'])} |")
    return "\n".join(linjer)


def sorter(lst):
    return sorted(lst, key=lambda t: (t["enhedspris"], t["fra"]))


def kort(t):
    return (f"{kr(t['enhedspris'])} kr/{t['enhed']} ({kr(t['stykpris'])} kr/stk) hos {t['butik']} – "
            f"{pakning(t)} for {kr(t['pris'])} kr, {periode(t)}")


def oversigt(config, aktuelle, i_dag, nu):
    idag = i_dag.isoformat()
    ud = ["# Tilbud", "",
          f"Opdateret {nu.strftime('%d/%m/%Y kl. %H:%M')}. Data fra eTilbudsavis. Sorteret efter "
          "pris pr. liter eller kg, så forskellige størrelser kan sammenlignes. Priserne er uden pant.", ""]
    for vare in config["varer"]:
        lst = [t for t in aktuelle if t["vare"] == vare["navn"]]
        gaelder = sorter([t for t in lst if t["fra"] <= idag <= t["til"]])
        kommende = sorter([t for t in lst if t["fra"] > idag])
        ud += [f"## {vare.get('emoji', '')} {vare['navn']}".replace("#  ", "# "), ""]
        if gaelder:
            ud += [f"> **Billigst lige nu:** {kort(gaelder[0])}", ""]

        e = vare.get("enhed", "l")
        ud += ["### Dag for dag", "", f"| Dag | Billigst | Kr/{e} | Kr/stk | Pakning | Næstbedst |",
               "|---|---|---:|---:|---|---|"]
        for n in range(14):
            d = (i_dag + timedelta(days=n)).isoformat()
            paa_dagen = sorter([t for t in lst if t["fra"] <= d <= t["til"]])
            if paa_dagen:
                a = paa_dagen[0]
                naest = ""
                if len(paa_dagen) > 1:
                    b = paa_dagen[1]
                    naest = f"{b['butik']} {kr(b['enhedspris'])} kr/{e} ({pakning(b)})"
                ud.append(f"| {kort_dato(d)} | {a['butik']} | **{kr(a['enhedspris'])}** "
                          f"| {kr(a['stykpris'])} | {pakning(a)} | {naest} |")
            else:
                ud.append(f"| {kort_dato(d)} | – | | | | |")
        ud += ["", "### Gælder nu", "", tabel(gaelder) if gaelder else "_Ingen lige nu._", "",
               "### Kommende", "", tabel(kommende) if kommende else "_Ingen kendte endnu._", ""]

    ud += ["---", "",
           "Type: „?“ betyder, at avisen ikke skriver dåse eller flaske. Grænsebutikker er ikke med. "
           "Kalenderen `tilbud.ics` og alarmen er beskrevet i `README.md`.", ""]
    return "\n".join(ud)


def ics_tekst(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def kalender(config, tilbud, nu):
    varer = {v["navn"]: v for v in config["varer"]}
    emoji = {v["navn"]: v.get("emoji", "") for v in config["varer"]}
    stempel = nu.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    ud = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//tilbudsjaeger//DA", "CALSCALE:GREGORIAN",
          "METHOD:PUBLISH", "X-WR-CALNAME:Tilbud", "X-WR-TIMEZONE:Europe/Copenhagen",
          "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for t in sorted(tilbud, key=lambda t: t["fra"]):
        if t["vare"] not in varer or under_graense(t, varer[t["vare"]], "kalender") is False:
            continue
        slut = date.fromisoformat(t["til"]) + timedelta(days=1)
        if t["enhed"] == "kg":
            titel = f"{emoji.get(t['vare'], '')} {t['vare']} {kr(t['stykpris'])} kr ({pakning(t)}) – {t['butik']}"
        else:
            titel = (f"{emoji.get(t['vare'], '')} {t['vare']} {kr(t['enhedspris'])} kr/l · "
                     f"{kr(t['stykpris'])} kr/stk – {t['butik']}")
        titel = titel.strip()
        beskr = f"{pakning(t)} for {kr(t['pris'])} kr (type: {t['type']}).\n" \
                f"{t['overskrift']}: {t['beskrivelse']}"
        if t["noter"]:
            beskr += "\nNoter: " + ", ".join(t["noter"])
        ud += ["BEGIN:VEVENT", f"UID:{hashlib.sha1(noegle(t).encode()).hexdigest()[:20]}@tilbudsjaeger", f"DTSTAMP:{stempel}",
               f"DTSTART;VALUE=DATE:{t['fra'].replace('-', '')}",
               f"DTEND;VALUE=DATE:{slut.isoformat().replace('-', '')}",
               f"SUMMARY:{ics_tekst(titel)}", f"DESCRIPTION:{ics_tekst(beskr)}",
               f"URL:{AVIS_LINK}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    ud.append("END:VCALENDAR")
    foldet = []  # RFC 5545: linjer på højst 75 bytes
    for linje in ud:
        b = linje.encode("utf-8")
        while len(b) > 75:
            cut = 75
            while (b[cut] & 0xC0) == 0x80:
                cut -= 1
            foldet.append(b[:cut].decode("utf-8"))
            b = b" " + b[cut:]
        foldet.append(b.decode("utf-8"))
    return "\r\n".join(foldet) + "\r\n"


def data_til_app(config, aktuelle, i_dag, nu):
    """Data til webappen og Home Assistant."""
    idag = i_dag.isoformat()
    varer = []
    for vare in config["varer"]:
        lst = sorter([t for t in aktuelle if t["vare"] == vare["navn"]])
        tilbud = [{
            "butik": t["butik"], "pris": t["pris"], "antal": t["antal"],
            **({"cl": t["cl"], "literpris": t["literpris"]} if t["enhed"] == "l"
               else {"gram": t["gram"], "kilopris": t["kilopris"]}),
            "maengde": t["maengde"], "enhed": t["enhed"], "enhedspris": t["enhedspris"],
            "pakning": pakning(t), "type": t["type"],
            "stykpris": t["stykpris"], "fra": t["fra"], "til": t["til"], "noter": t["noter"],
            "beskrivelse": f"{t['overskrift']}: {t['beskrivelse']}",
            "gaelder_nu": t["fra"] <= idag <= t["til"], "foerst_set": t.get("foerst_set"),
        } for t in lst]
        nu_ = [t for t in tilbud if t["gaelder_nu"]]
        varer.append({"navn": vare["navn"], "emoji": vare.get("emoji", ""),
                      "enhed": vare.get("enhed", "l"),
                      "alarm_max_literpris": vare.get("alarm_max_literpris"),
                      "alarm_max_pris": vare.get("alarm_max_pris"),
                      "normal_literpris": vare.get("normal_literpris"),
                      "billigst_nu": nu_[0] if nu_ else None,
                      "antal_nu": len(nu_), "tilbud": tilbud})
    return {"opdateret": nu.isoformat(timespec="minutes"), "i_dag": idag, "varer": varer}


# ---------------------------------------------------------------- main

def main():
    config = json.loads((MAPPE / "config.json").read_text(encoding="utf-8"))
    nu = datetime.now(TZ)
    i_dag = nu.date()
    idag = i_dag.isoformat()

    historik_fil = DATA / "tilbud.json"
    historik = json.loads(historik_fil.read_text(encoding="utf-8")) if historik_fil.exists() else {}
    historik = {noegle(t): normaliser(t) for t in historik.values()}  # ældre filer brugte ID som nøgle

    if len(sys.argv) > 1:  # test med gemte API-svar: tjek_tilbud.py fil1.json fil2.json
        raa = list({o["id"]: o for f in sys.argv[1:]
                    for o in json.loads(Path(f).read_text())}.values())
    else:
        raa = hent(config)

    relevante = []
    for o in raa:
        if er_graense(o, config):
            continue
        for vare in config["varer"]:
            t = fortolk(o, vare)
            if t and vurder(t, vare):
                relevante.append(t)
                break

    varer = {v["navn"]: v for v in config["varer"]}
    nye = [t for t in relevante
           if noegle(t) not in historik and t["til"] >= idag
           and under_graense(t, varer[t["vare"]]) is True]

    for t in relevante:
        n = noegle(t)
        t["foerst_set"] = historik.get(n, {}).get("foerst_set", idag)
        historik[n] = t

    # Gem 60 dage bagud; vis kun dem, der stadig gælder
    graense = (i_dag - timedelta(days=60)).isoformat()
    historik = {k: v for k, v in historik.items() if v["til"] >= graense}
    aktuelle = [t for t in historik.values() if t["til"] >= idag]

    DATA.mkdir(exist_ok=True)
    historik_fil.write_text(json.dumps(historik, ensure_ascii=False, indent=1, sort_keys=True)
                            + "\n", encoding="utf-8")
    (MAPPE / "OVERSIGT.md").write_text(oversigt(config, aktuelle, i_dag, nu), encoding="utf-8")
    (MAPPE / "aktuelle.json").write_text(
        json.dumps(data_til_app(config, aktuelle, i_dag, nu), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    (MAPPE / "tilbud.ics").write_text(kalender(config, historik.values(), nu), encoding="utf-8",
                                      newline="")

    nye_fil, titel_fil = DATA / "nye.md", DATA / "nye_titel.txt"
    if nye:
        nye = sorter(nye)
        dele = []
        for vare in config["varer"]:
            mine = [t for t in nye if t["vare"] == vare["navn"]]
            if mine:
                dele += [f"**{vare['navn']}** – nye tilbud til {graense_tekst(vare)}:", "",
                         tabel(mine), ""]
        dele.append("Se hele overblikket i `OVERSIGT.md`.")
        nye_fil.write_text("\n".join(dele) + "\n", encoding="utf-8")
        b = nye[0]
        titel_fil.write_text(
            f"{b['vare']}: {kr(b['enhedspris'])} kr/{b['enhed']} ({kr(b['stykpris'])} kr/stk, {pakning(b)}) "
            f"hos {b['butik']}, {periode(b)}"
            + (f" + {len(nye) - 1} andre nye" if len(nye) > 1 else ""), encoding="utf-8")
    else:
        for f in (nye_fil, titel_fil):
            if f.exists():
                f.unlink()

    print(f"{len(raa)} tilbud hentet, {len(relevante)} relevante, {len(nye)} nye under alarmgrænsen.")
    for t in sorted(aktuelle, key=lambda t: (t["vare"], t["enhedspris"]))[:30]:
        print(f"  {t['vare']:<14} {kr(t['enhedspris']):>6} kr/{t['enhed']:<2} {kr(t['stykpris']):>6} kr/stk  "
              f"{t['butik']:<14} {pakning(t):<12} {t['type']:<6} {periode(t)}")


if __name__ == "__main__":
    main()
