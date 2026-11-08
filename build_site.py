#!/usr/bin/env python3
"""Render docs/index.html from site.json and memories.toml.

The page is a Leaflet map of Kauai with one pin per place, a memory panel
that shows the story for the selected pin, and a sortable list of every place.

    python3 build_site.py                     # memories.toml -> docs/index.html
    python3 build_site.py --memories F --out F

The build checks every entry in the memories file first. When an entry is
wrong it prints each problem and exits 1 without touching the page.
"""
import argparse
import html
import json
import math
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
SITE = json.loads((BASE / "site.json").read_text())
HOME_TZ = ZoneInfo(SITE["timezones"][0]["id"])
NOW = datetime.now(timezone.utc)
CATEGORIES = {c["id"]: c for c in SITE["categories"]}
PHOTOS_JSON = BASE / "photos.json"
# Set by main(): True while rendering the public page, which leaves out photo GPS.
PUBLIC = True

# Kauai with a margin. A pin outside this box is a typo in lat or lng.
LAT_RANGE = (21.80, 22.30)
LNG_RANGE = (-159.85, -159.25)
REQUIRED = ("name", "town", "category", "status", "lat", "lng")
OPTIONAL = ("spot", "memory", "years", "closed_year", "now", "history", "approximate", "id")
STRING_FIELDS = ("name", "town", "spot", "category", "status", "memory", "years", "closed_year", "now", "id")
# Element ids on the page. A place id must not reuse one, or its link breaks.
RESERVED_IDS = {"map", "explore", "places", "panel", "memory-empty", "places-data", "loose"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def stamp(dt, fmt="%Y-%m-%d %-I:%M %p"):
    """Format a datetime in the home time zone for footer text."""
    return dt.astimezone(HOME_TZ).strftime(fmt)


def link(href, text, cls=""):
    """External links open in a new tab. Same-page anchors do not."""
    if href.startswith("#"):
        return f'<a href="{esc(href)}"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{esc(text)}</a>'
    return f'<a href="{esc(href)}" target="_blank" rel="noopener"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{esc(text)}</a>'


def table(headers, rows, lead_cols=1, caption=None, sortable=True, row_attrs=None):
    """A table whose first `lead_cols` columns are left-aligned and whose data
    columns are centered, header cells included. Headers sort on click unless
    `sortable=False`. Pass cells as HTML strings; put a `data-sort` attribute
    on a cell when its visible text is not what should sort (a date, a rank).
    `row_attrs` holds one attribute string per row, for example `data-id="x"`."""
    th = "".join(f'<th{" class=lead" if i < lead_cols else ""}{" role=button tabindex=0 aria-sort=none" if sortable else ""}>{h}</th>' for i, h in enumerate(headers))
    body = []
    for n, row in enumerate(rows):
        td = "".join(f'<td{" class=lead" if i < lead_cols else ""}>{c}</td>' for i, c in enumerate(row))
        attrs = f" {row_attrs[n]}" if row_attrs else ""
        body.append(f"<tr{attrs}>{td}</tr>")
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return f'<div class="tablewrap"><table{" class=sortable" if sortable else ""}>{cap}<thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def twisty(title, inner, count=None, open_=True, id_=None):
    """A section the reader can collapse. All twisties reopen on refresh."""
    badge = f' <span class="count" data-count>{esc(count)}</span>' if count is not None else ""
    ident = f' id="{esc(id_)}"' if id_ else ""
    return f'<details class="group"{ident}{" open" if open_ else ""}><summary><h2>{esc(title)}{badge}</h2></summary>{inner}</details>'


# ---------------------------------------------------------------------------
# Memories file: load and check.
# ---------------------------------------------------------------------------

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def natural(text):
    """Sort key that puts "Unnamed place 2" before "Unnamed place 10"."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_place(p, label):
    """Return a list of problems with one [[place]] entry."""
    problems = []
    unknown = sorted(set(p) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        problems.append(f"{label}: unknown field(s) {', '.join(unknown)}")
    for key in REQUIRED:
        if key not in p or (isinstance(p[key], str) and not p[key].strip()):
            problems.append(f"{label}: missing {key}")
    for key in STRING_FIELDS:
        if key in p and not isinstance(p[key], str):
            problems.append(f"{label}: {key} must be text in quotes")
    if isinstance(p.get("town"), str) and "," in p["town"]:
        problems.append(f"{label}: town {p['town']!r} should be the town only; put the landmark or street in spot")
    if "category" in p and p["category"] not in CATEGORIES:
        problems.append(f"{label}: category {p['category']!r} is not one of {', '.join(CATEGORIES)}")
    if "status" in p and p["status"] not in ("open", "closed", "unknown"):
        problems.append(f"{label}: status must be \"open\", \"closed\", or \"unknown\"")
    if p.get("status") == "open":
        if "closed_year" in p:
            problems.append(f"{label}: closed_year is set but status is \"open\"")
    for key, (lo, hi) in (("lat", LAT_RANGE), ("lng", LNG_RANGE)):
        if key in p:
            if not is_number(p[key]):
                problems.append(f"{label}: {key} must be a number without quotes")
            elif not lo <= p[key] <= hi:
                problems.append(f"{label}: {key} {p[key]} is off the island (expected {lo} to {hi})")
    if "history" in p and not (isinstance(p["history"], list) and p["history"] and all(isinstance(h, str) and h.strip() for h in p["history"])):
        problems.append(f"{label}: history must be a list of names in quotes, earliest first")
    if "approximate" in p and not isinstance(p["approximate"], bool):
        problems.append(f"{label}: approximate must be true or false without quotes")
    return problems


def load_places(path):
    """Read and check the memories file. Exit 1 and list every problem if any entry is wrong."""
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"{path.name}: not valid TOML: {exc}")
    entries = raw.get("place", [])
    problems, places, seen = [], [], {}
    for n, p in enumerate(entries, 1):
        label = f"place {n} ({p.get('name', 'no name')})"
        found = check_place(p, label)
        problems += found
        if found:
            continue
        pid = p.get("id") or slug(p["name"])
        if not ID_RE.match(pid):
            problems.append(f"{label}: id {pid!r} must be lowercase letters, digits, and hyphens")
        elif pid in RESERVED_IDS:
            problems.append(f"{label}: id {pid!r} is used by the page; set a different id")
        elif pid in seen:
            problems.append(f"{label}: id {pid!r} repeats place {seen[pid]}; set a different id on one of them")
        seen.setdefault(pid, n)
        paragraphs = [" ".join(block.split()) for block in re.split(r"\n\s*\n", p.get("memory", "").strip()) if block.strip()]
        places.append({**p, "id": pid, "paragraphs": paragraphs})
    if problems:
        print(f"{path.name}: {len(problems)} problem(s); the page was not rebuilt.", file=sys.stderr)
        print(*problems, sep="\n", file=sys.stderr)
        sys.exit(1)
    untold = [p["name"] for p in places if not p["paragraphs"] and p["status"] != "unknown"]
    if untold:
        print(f"{len(untold)} place(s) with no memory written yet: {', '.join(untold)}")
    return sorted(places, key=lambda p: natural(p["name"]))


# ---------------------------------------------------------------------------
# Page furniture.
# ---------------------------------------------------------------------------

CSS = r"""
:root{color-scheme:light;--bg:#f4f3ef;--veil:rgba(244,243,239,.88);--surface:#ffffff;--surface2:#ecebe5;--line:#d8d5cb;--ink:#1e2229;--muted:#545a64;--faint:#8c939d;--link:#1f4e79;--accent:ACCENT_LIGHT;--chip:#e9e7df;CAT_LIGHT
--s1:4px;--s2:8px;--s3:12px;--s4:16px;--s5:24px;--s6:40px;--pill:36px;--fs-sm:.875rem;--fs-md:1rem;--fs-base:1rem;
/* Brand layer. Source: ~/.claude/skills/design/assets/brand.css (version 2026-11-08). */
--font-sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--font-mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--fs-caption:.75rem;--fs-small:.875rem;--fs-body:1rem;--fs-h3:1.25rem;--fs-h2:1.5rem;--fs-h1:2rem;--control-h:36px;--control-gap:8px}
:root[data-theme="dark"]{color-scheme:dark;--bg:#15181d;--veil:rgba(21,24,29,.84);--surface:#1d2128;--surface2:#252a32;--line:#363c46;--ink:#e7e9ec;--muted:#a7adb7;--faint:#7d848f;--link:#8ab8e6;--accent:ACCENT_DARK;--chip:#2e343d;CAT_DARK}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#15181d;--veil:rgba(21,24,29,.84);--surface:#1d2128;--surface2:#252a32;--line:#363c46;--ink:#e7e9ec;--muted:#a7adb7;--faint:#7d848f;--link:#8ab8e6;--accent:ACCENT_DARK;--chip:#2e343d;CAT_DARK}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:400 16px/1.5 var(--font-sans)}
code,kbd,samp,pre{font-family:var(--font-mono)}:not(pre)>code{font-size:.875em}
h1,h2,h3,h4,strong,b,th{font-weight:600}h1,h2{letter-spacing:-.01em}
/* Satellite view of Kauai under a veil of the page color. The veil strength keeps every text color at 4.5:1 or better
   even over the darkest (light theme) or brightest (dark theme) pixel of the image. */
body::before{content:"";position:fixed;inset:0;z-index:-1;background:linear-gradient(var(--veil),var(--veil)),url(kauai-satellite.jpg) center/cover no-repeat;pointer-events:none}
@media (prefers-contrast:more),(forced-colors:active){body::before{display:none}}
a{color:var(--link)}button{font:inherit;cursor:pointer}
main{max-width:1180px;margin:0 auto;padding:var(--s5) var(--s4) 64px}
h1{font-size:var(--fs-h1);line-height:1.1;margin:0 0 var(--s2)}
h2{font-size:var(--fs-h2);margin:var(--s6) 0 var(--s3);padding-bottom:var(--s1);border-bottom:1px solid var(--line)}
h3{font-size:var(--fs-h3);line-height:1.2;margin:0 0 var(--s1)}
p{margin:0 0 var(--s3)}.sub{color:var(--muted);max-width:760px}.small{font-size:var(--fs-sm);color:var(--muted)}
.top{display:flex;flex-direction:column;align-items:flex-start;gap:var(--s4)}
.controls{display:flex;gap:var(--s4);align-items:flex-start;flex-wrap:wrap}.ctl{display:flex;flex-direction:column;align-items:flex-start;gap:var(--s1)}
.ctlhead{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap}
.bulk{border:0;background:transparent;color:var(--link);text-decoration:underline;text-underline-offset:2px;font-size:var(--fs-sm);padding:6px var(--s2);min-height:32px}
.ctl>.seglabel,.ctlhead{min-height:32px;display:flex;align-items:center}
.seglabel{font-size:var(--fs-caption);font-weight:500;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);padding-left:2px}
.seg{display:inline-flex;align-items:stretch;border:1px solid var(--line);border-radius:999px;overflow:hidden;background:var(--surface)}
.seg button{border:0;background:transparent;color:var(--muted);height:calc(var(--control-h) - 2px);padding:0 14px;font-size:var(--fs-md);min-width:44px}.seg button+button{border-left:1px solid var(--line)}
.seg button.active{background:var(--accent);color:#fff;font-weight:600}.seg button.active::before{content:"\2713\00a0"}
:root[data-theme="dark"] .seg button.active{color:#15181d}@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .seg button.active{color:#15181d}}
.cats{display:flex;flex-wrap:wrap;gap:var(--s2)}
.cats button::before{content:"";display:inline-block;width:1em;text-align:center;margin-right:-2px}
.cats .lbl{display:inline-flex;flex-direction:column}.cats .lbl::after{content:attr(data-t);font-weight:600;height:0;overflow:hidden;visibility:hidden}
.cats button{display:inline-flex;align-items:center;gap:6px;height:var(--pill);border:1px solid var(--line);background:var(--surface);color:var(--muted);border-radius:999px;padding:0 var(--s3);font-size:var(--fs-md)}
.cats button[aria-pressed="true"]{color:var(--ink);font-weight:600;border-color:var(--ink)}.cats button[aria-pressed="true"]::before{content:"\2713"}
.cats button[aria-pressed="false"] .dot{background:transparent;border-color:var(--faint)}
.dot{display:inline-block;width:.75em;height:.75em;border-radius:50%;background:var(--cat);border:2px solid var(--cat);flex:none}.dot.closed{background:transparent}
.cat-food{--cat:var(--c-food)}CAT_CLASSES
.legend{display:flex;flex-wrap:wrap;align-items:center;gap:var(--s1) var(--s4);margin:var(--s3) 0 0}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend .dot{--cat:var(--muted)}
.explore{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:var(--s4);margin-top:var(--s4)}
#map{height:72vh;min-height:440px;border:1px solid var(--line);border-radius:14px;background:var(--surface2);z-index:0}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:var(--s4) var(--s5);max-height:72vh;overflow:auto}
.panel .where{color:var(--muted);margin:0 0 var(--s3)}
.chips{display:flex;flex-wrap:wrap;gap:var(--s1) var(--s2);margin:0 0 var(--s3)}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--chip);border-radius:999px;padding:2px 10px;font-size:var(--fs-sm);color:var(--ink)}
.facts{display:grid;grid-template-columns:auto 1fr;gap:var(--s1) var(--s3);margin:0 0 var(--s3);font-size:var(--fs-md)}
.facts dt{color:var(--muted)}.facts dd{margin:0}
.histlabel{margin:0 0 var(--s1);font-weight:600}
.history{margin:0 0 var(--s4);padding-left:1.4em}.history li{margin:0 0 2px}.history li:last-child{font-weight:600}
.story p{font-size:var(--fs-body);line-height:1.6}
.pager{display:flex;align-items:center;justify-content:space-between;gap:var(--s2);margin-top:var(--s4);padding-top:var(--s3);border-top:1px solid var(--line)}
.panel .pager{margin:0 0 var(--s4);padding:0 0 var(--s3);border-top:0;border-bottom:1px solid var(--line)}
.pager button{height:var(--pill);border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:0 var(--s4);font-size:var(--fs-md)}
.pager button:disabled{opacity:.4;cursor:default}
.pin{fill:var(--cat);fill-opacity:1;stroke:#fff;stroke-width:2.5;cursor:pointer}
.pin.closed{fill:var(--surface);stroke:var(--cat);stroke-width:3.5}
.pin.sel{stroke:var(--ink);stroke-width:4}.pin.closed.sel{stroke:var(--cat);stroke-width:6}
.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--s2);margin:0 0 var(--s3)}
.thumb{padding:0;border:0;background:var(--surface2);border-radius:8px;overflow:hidden;aspect-ratio:1/1}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumbs.wide{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:var(--s2)}.loose{margin:0}.loose .thumb{width:100%}
.loose figcaption{font-size:var(--fs-sm);font-weight:600;text-align:center;padding-top:2px}.loose .ttl{display:block;font-weight:400;color:var(--muted);line-height:1.25}
.dot.dashed{background:transparent;border-style:dashed}
.pin.cat-unnamed{fill:var(--surface);stroke:var(--cat);stroke-width:3;stroke-dasharray:4 3}.pin.cat-unnamed.sel{stroke-width:5}
#viewer{border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--ink);padding:var(--s3);width:min(960px,96vw);max-height:96vh}
#viewer::backdrop{background:rgba(0,0,0,.75)}
.flip{position:relative;perspective:1600px}
.face{transition:transform .5s;backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:10px}
.front{position:relative;background:#000;display:flex;justify-content:center;min-height:240px}
.front img{max-width:100%;max-height:72vh;display:block;object-fit:contain}
.back{position:absolute;inset:0;transform:rotateY(180deg);background:var(--surface2);padding:var(--s5) var(--s4);overflow:auto}
.flip.flipped .front{transform:rotateY(-180deg)}.flip.flipped .back{transform:rotateY(0)}
.info{position:absolute;top:var(--s2);right:var(--s2);width:40px;height:40px;border-radius:50%;border:1px solid rgba(255,255,255,.75);background:rgba(0,0,0,.55);color:#fff;font:600 var(--fs-h3)/1 var(--font-sans)}
.back .info{background:var(--surface);color:var(--ink);border-color:var(--line);font:600 var(--fs-body)/1 var(--font-sans)}
#v-meta{margin-right:var(--s6)}#v-meta dd:first-of-type{font-weight:600;font-size:var(--fs-h3)}
.vcap{margin:var(--s2) 0 0}
@media (prefers-reduced-motion:reduce){.face{transition:none}}
.leaflet-container{font:inherit}.mapview{box-shadow:0 1px 4px rgba(0,0,0,.3)}.mapview button{padding:0 12px}.leaflet-tooltip{font-size:var(--fs-sm)}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:14px}
table{border-collapse:collapse;width:100%;font-size:var(--fs-md)}caption{text-align:left;padding:var(--s3) var(--s4) 0;font-weight:600}
th,td{padding:var(--s2) var(--s3);border-top:1px solid var(--line);text-align:center;white-space:nowrap}
thead th{border-top:0;background:var(--surface2);font-weight:600;color:var(--muted)}
th.lead,td.lead{text-align:left}td.lead{font-weight:600}
.sortable th{cursor:pointer;user-select:none}.sortable th::after{content:"";display:inline-block;width:.9em;color:var(--faint)}.sortable th[aria-sort="ascending"]::after{content:"\25B4"}.sortable th[aria-sort="descending"]::after{content:"\25BE"}
.group summary{list-style:none;cursor:pointer}.group summary::-webkit-details-marker{display:none}.group summary h2{display:inline-block;width:100%}
.group summary h2::before{content:"\25BE";display:inline-block;width:1.1em;color:var(--muted)}.group:not([open]) summary h2::before{content:"\25B8"}
.count{font-size:var(--fs-sm);font-weight:600;color:var(--muted);margin-left:var(--s2)}
footer{margin-top:var(--s6);padding-top:var(--s4);border-top:1px solid var(--line);font-size:var(--fs-sm);color:var(--muted)}
@media (max-width:899px){.explore{grid-template-columns:1fr}#map{height:58vh;min-height:320px}.panel{max-height:none}}
@media (max-width:640px){main{padding:var(--s4) var(--s4) 48px}h1{font-size:var(--fs-h2)}.controls{width:100%}.panel{padding:var(--s4)}}
"""

THEME_JS = r"""
(function(){
const root=document.documentElement,KEY='SITE_KEY';
// Theme: the viewer's choice wins; otherwise follow the device. Dark Reader is locked out in <head>.
const savedTheme=(()=>{try{return localStorage.getItem(KEY+'-theme')}catch(e){return null}})();
if(savedTheme==='dark'||savedTheme==='light')root.setAttribute('data-theme',savedTheme);
function isDark(){const t=root.getAttribute('data-theme');return t==='dark'||(!t&&matchMedia('(prefers-color-scheme:dark)').matches)}
window.isDark=isDark;
function paintTheme(){const dark=isDark();document.querySelectorAll('.th').forEach(b=>b.classList.toggle('active',(b.dataset.theme==='dark')===dark));document.querySelectorAll('meta[name=theme-color]').forEach(m=>{m.removeAttribute('media');m.content=dark?'#15181d':'#f4f3ef'});}
document.querySelectorAll('.th').forEach(b=>b.addEventListener('click',()=>{root.setAttribute('data-theme',b.dataset.theme);try{localStorage.setItem(KEY+'-theme',b.dataset.theme)}catch(e){}paintTheme();}));
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',paintTheme);
paintTheme();
// Sortable tables: click a header to sort by that column; click again to reverse.
function cellKey(td){const raw=td.dataset.sort!==undefined?td.dataset.sort:td.textContent.trim();const n=parseFloat(raw.replace(/[,%#$]/g,''));return isNaN(n)||!/^[-+#$]?[\d.,]+%?$/.test(raw.replace(/\s/g,''))?raw.toLowerCase():n;}
document.querySelectorAll('table.sortable').forEach(t=>{
  const ths=[...t.querySelectorAll('thead th')],body=t.tBodies[0];if(!body)return;
  ths.forEach((th,i)=>{const go=()=>{const dir=th.getAttribute('aria-sort')==='ascending'?'descending':'ascending';ths.forEach(h=>h.setAttribute('aria-sort','none'));th.setAttribute('aria-sort',dir);
    const rows=[...body.rows];rows.sort((a,b)=>{const x=cellKey(a.cells[i]),y=cellKey(b.cells[i]);const r=typeof x==='number'&&typeof y==='number'?x-y:typeof x==='number'?-1:typeof y==='number'?1:String(x).localeCompare(String(y));return dir==='ascending'?r:-r;});
    rows.forEach(r=>body.appendChild(r));};
    th.addEventListener('click',go);th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});});
});
})();
"""

MAP_JS = r"""
(function(){
const places=JSON.parse(document.getElementById('places-data').textContent);
const byId=Object.fromEntries(places.map(p=>[p.id,p]));
const root=document.documentElement,panel=document.getElementById('panel'),empty=document.getElementById('memory-empty');
const narrow=matchMedia('(max-width:899px)');
// Map: Kauai fills the frame; panning stops a little past the coast.
const island=L.latLngBounds([21.86,-159.80],[22.24,-159.28]);
const map=L.map('map',{maxBounds:island.pad(0.6),minZoom:9,maxZoom:19,zoomSnap:0.5});
map.fitBounds(island);
map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
const attr='Tiles &copy; <a href="https://www.esri.com" target="_blank" rel="noopener">Esri</a>, HERE, Garmin, USGS, NGA, EPA, NPS, &copy; OpenStreetMap contributors';
const ESRI='https://server.arcgisonline.com/ArcGIS/rest/services/';
// Map view: Esri World Topo (light) or Dark Gray with labels (dark), following the theme.
// Satellite view: Esri World Imagery with road and place-name overlays, the same in both themes. No layer needs a key.
const layers={
  light:L.layerGroup([L.tileLayer(ESRI+'World_Topo_Map/MapServer/tile/{z}/{y}/{x}',{attribution:attr,maxZoom:19})]),
  dark:L.layerGroup([L.tileLayer(ESRI+'Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',{attribution:attr,maxZoom:19,maxNativeZoom:16}),
                     L.tileLayer(ESRI+'Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,maxNativeZoom:16})]),
  satellite:L.layerGroup([L.tileLayer(ESRI+'World_Imagery/MapServer/tile/{z}/{y}/{x}',{attribution:'Imagery &copy; <a href="https://www.esri.com" target="_blank" rel="noopener">Esri</a>, Maxar, Earthstar Geographics',maxZoom:19}),
                          L.tileLayer(ESRI+'Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}',{maxZoom:19}),
                          L.tileLayer(ESRI+'Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',{maxZoom:19})])};
const VIEW_KEY='SITE_KEY-view';
let view=(()=>{try{return localStorage.getItem(VIEW_KEY)}catch(e){return null}})()==='satellite'?'satellite':'map';
let mode=null;
function syncTiles(){
  const m=view==='satellite'?'satellite':(isDark()?'dark':'light');
  if(m===mode)return;
  if(mode)map.removeLayer(layers[mode]);
  mode=m;layers[m].addTo(map);
  document.querySelectorAll('.view').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
}
// The Map/Satellite switch sits on the map, top right, so it stays next to what it changes on a phone.
const ViewControl=L.Control.extend({options:{position:'topright'},onAdd(){
  const box=L.DomUtil.create('div','seg mapview');box.setAttribute('role','group');box.setAttribute('aria-label','Map view');
  box.innerHTML='<button type="button" class="view" data-view="map">Map</button><button type="button" class="view" data-view="satellite">Satellite</button>';
  L.DomEvent.disableClickPropagation(box);
  box.querySelectorAll('.view').forEach(b=>b.addEventListener('click',()=>{view=b.dataset.view;try{localStorage.setItem(VIEW_KEY,view)}catch(e){}syncTiles();}));
  return box;}});
new ViewControl().addTo(map);
syncTiles();
new MutationObserver(syncTiles).observe(root,{attributes:true,attributeFilter:['data-theme']});
matchMedia('(prefers-color-scheme:dark)').addEventListener('change',syncTiles);
// Pins: color comes from CSS (.cat-*), so pins follow the theme. Closed places are hollow.
const R=9,RSEL=13,markers={};
places.forEach(p=>{
  const m=L.circleMarker([p.lat,p.lng],{radius:R,className:'pin cat-'+p.category+(p.status==='closed'?' closed':''),bubblingMouseEvents:false});
  m.bindTooltip(p.name,{direction:'top',offset:[0,-R]});
  m.on('click',()=>select(p.id,'pin'));
  markers[p.id]=m;
});
// Filters: status segment plus category toggles. Pins and table rows follow both.
let show='all';const catOn=new Set(places.map(p=>p.category)),townOn=new Set(places.map(p=>p.town));
function visible(p){return (show==='all'||p.status===show)&&catOn.has(p.category)&&townOn.has(p.town)}
function shown(){return places.filter(visible)}
function applyFilters(){
  places.forEach(p=>{const on=visible(p),m=markers[p.id];if(on&&!map.hasLayer(m))m.addTo(map);if(!on&&map.hasLayer(m))m.remove();});
  document.querySelectorAll('#places tbody tr').forEach(tr=>{tr.hidden=!visible(byId[tr.dataset.id])});
  const c=document.querySelector('#places [data-count]');if(c)c.textContent=shown().length;
  document.querySelectorAll('.show').forEach(b=>b.classList.toggle('active',b.dataset.show===show));
  document.querySelectorAll('button[data-cat]').forEach(b=>b.setAttribute('aria-pressed',catOn.has(b.dataset.cat)));
  document.querySelectorAll('button[data-town]').forEach(b=>b.setAttribute('aria-pressed',townOn.has(b.dataset.town)));
  if(current&&markers[current]&&map.hasLayer(markers[current]))mark(current,true);
  pager();
}
document.querySelectorAll('.show').forEach(b=>b.addEventListener('click',()=>{show=b.dataset.show;applyFilters();}));
document.querySelectorAll('button[data-cat]').forEach(b=>b.addEventListener('click',()=>{const c=b.dataset.cat;catOn.has(c)?catOn.delete(c):catOn.add(c);applyFilters();}));
document.querySelectorAll('button[data-town]').forEach(b=>b.addEventListener('click',()=>{const c=b.dataset.town;townOn.has(c)?townOn.delete(c):townOn.add(c);applyFilters();}));
document.querySelectorAll('.bulk').forEach(b=>b.addEventListener('click',()=>{const k=b.dataset.bulk,set=k==='cat'?catOn:townOn;set.clear();
  if(b.dataset.on==='1')document.querySelectorAll(`button[data-${k}]`).forEach(x=>set.add(x.dataset[k]));applyFilters();}));
// Selection: one memory card shows at a time; the URL hash names it so a link opens it.
let current=null;
function mark(id,on){const m=markers[id];m.setRadius(on?RSEL:R);const el=m.getElement();if(el)el.classList.toggle('sel',on);if(on)m.bringToFront();}
function select(id,source){
  const p=byId[id];if(!p)return;
  if(current&&current!==id)mark(current,false);
  current=id;
  document.querySelectorAll('.memory').forEach(a=>{a.hidden=a.dataset.id!==id});
  if(empty)empty.hidden=true;
  if(!map.hasLayer(markers[id]))markers[id].addTo(map);
  mark(id,true);
  if(source!=='pin')map.flyTo([p.lat,p.lng],Math.max(map.getZoom(),16),{duration:.6});
  if(location.hash!=='#'+id)history.replaceState(null,'','#'+id);
  panel.scrollTop=0;
  if(source==='table')document.querySelector('.explore').scrollIntoView({behavior:'smooth',block:'start'});
  else if(source!=='load'&&narrow.matches)panel.scrollIntoView({behavior:'smooth',block:'nearest'});
  pager();
}
// Previous and next walk the places that pass the filters, in name order.
const prev=document.getElementById('prev'),next=document.getElementById('next'),pos=document.getElementById('pos');
function pager(){
  if(!prev)return;const list=shown(),i=list.findIndex(p=>p.id===current);
  prev.disabled=i<=0;next.disabled=i<0?list.length===0:i>=list.length-1;
  pos.textContent=i<0?list.length+' places':(i+1)+' of '+list.length;
}
if(prev){
  prev.addEventListener('click',()=>{const l=shown(),i=l.findIndex(p=>p.id===current);if(i>0)select(l[i-1].id,'pager');});
  next.addEventListener('click',()=>{const l=shown(),i=l.findIndex(p=>p.id===current);if(i<l.length-1)select(l[i+1].id,'pager');});
}
document.querySelectorAll('a.go').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();select(a.dataset.id,'table');}));
window.addEventListener('hashchange',()=>select(decodeURIComponent(location.hash.slice(1)),'hash'));
applyFilters();
const start=decodeURIComponent(location.hash.slice(1));
if(byId[start])select(start,'load');
// Photo viewer: a thumbnail opens the dialog; the "i" button flips the photo over to its details.
// The flip stays on while you page through, so you can read details photo after photo.
const photos=JSON.parse(document.getElementById('photos-data').textContent);
const dlg=document.getElementById('viewer'),vImg=document.getElementById('v-img'),vCap=document.getElementById('v-cap'),vMeta=document.getElementById('v-meta'),vMaps=document.getElementById('v-maps'),vFlip=document.getElementById('v-flip'),vInfo=document.getElementById('v-info');
let strip=[],at=0;
function flip(on){vFlip.classList.toggle('flipped',on);vInfo.setAttribute('aria-pressed',on);}
function showPhoto(i){
  at=i;const id=strip[i],ph=photos[id];
  vImg.src=ph.full;vImg.alt='Photo '+id;vCap.textContent=ph.cap+' ('+(i+1)+' of '+strip.length+')';
  vMeta.replaceChildren(...ph.info.flatMap(([k,v])=>{const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=v;return [dt,dd];}));
  vMaps.replaceChildren();
  if(ph.maps){const a=document.createElement('a');a.href=ph.maps;a.target='_blank';a.rel='noopener';a.textContent='Open where this photo was taken in Google Maps';vMaps.append(a);}
  document.getElementById('v-prev').disabled=i===0;document.getElementById('v-next').disabled=i===strip.length-1;
}
document.querySelectorAll('.thumb').forEach(b=>b.addEventListener('click',()=>{
  strip=[...b.closest('.thumbs').querySelectorAll('.thumb')].map(t=>t.dataset.pid);
  flip(false);showPhoto(strip.indexOf(b.dataset.pid));dlg.showModal();}));
vInfo.addEventListener('click',()=>flip(true));
document.getElementById('v-front').addEventListener('click',()=>flip(false));
document.getElementById('v-prev').addEventListener('click',()=>{if(at>0)showPhoto(at-1)});
document.getElementById('v-next').addEventListener('click',()=>{if(at<strip.length-1)showPhoto(at+1)});
document.getElementById('v-close').addEventListener('click',()=>dlg.close());
dlg.addEventListener('click',e=>{if(e.target===dlg)dlg.close()});
dlg.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'&&at>0)showPhoto(at-1);else if(e.key==='ArrowRight'&&at<strip.length-1)showPhoto(at+1);else if(e.key==='i')flip(!vFlip.classList.contains('flipped'));});
})();
"""


def cat_css():
    """Category colors as tokens for both themes, plus one class per category."""
    light = "".join(f"--c-{c['id']}:{c['light']};" for c in SITE["categories"])
    dark = "".join(f"--c-{c['id']}:{c['dark']};" for c in SITE["categories"])
    classes = "".join(f".cat-{c['id']}{{--cat:var(--c-{c['id']})}}" for c in SITE["categories"] if c["id"] != "food")
    return light, dark, classes


def bulk_head(kind, label, noun):
    """A filter group's label with Select all and Deselect all buttons beside it."""
    return (f'<div class="ctlhead"><span class="seglabel" id="lbl-{kind}s">{label}</span>'
            f'<button type="button" class="bulk" data-bulk="{kind}" data-on="1" aria-label="Select all {noun}">Select all</button>'
            f'<button type="button" class="bulk" data-bulk="{kind}" data-on="0" aria-label="Deselect all {noun}">Deselect all</button></div>')


def controls(places):
    theme = ('<div class="ctl"><span class="seglabel" id="lbl-theme">Theme</span>'
             '<div class="seg" role="group" aria-labelledby="lbl-theme"><button type="button" class="th" data-theme="light">Light</button>'
             '<button type="button" class="th" data-theme="dark">Dark</button></div></div>')
    if not places:
        return f'<div class="controls">{theme}</div>'
    show = ('<div class="ctl"><span class="seglabel" id="lbl-show">Show</span>'
            '<div class="seg" role="group" aria-labelledby="lbl-show">'
            '<button type="button" class="show" data-show="all">All</button>'
            '<button type="button" class="show" data-show="open">Still here</button>'
            '<button type="button" class="show" data-show="closed">Closed</button></div></div>')
    used = [c for c in SITE["categories"] if any(p["category"] == c["id"] for p in places)]
    buttons = "".join(f'<button type="button" data-cat="{esc(c["id"])}" aria-pressed="true"><span class="dot cat-{esc(c["id"])}"></span><span class="lbl" data-t="{esc(c["label"])}">{esc(c["label"])}</span></button>' for c in used)
    cats = f'<div class="ctl">{bulk_head("cat", "Categories", "categories")}<div class="cats" role="group" aria-labelledby="lbl-cats">{buttons}</div></div>'
    # Towns run west to east, by the average longitude of their places.
    lngs = {}
    for p in places:
        lngs.setdefault(p["town"], []).append(p["lng"])
    order = sorted(lngs, key=lambda t: sum(lngs[t]) / len(lngs[t]))
    tbuttons = "".join(f'<button type="button" data-town="{esc(t)}" aria-pressed="true"><span class="lbl" data-t="{esc(t)}">{esc(t)}</span></button>' for t in order)
    towns = f'<div class="ctl">{bulk_head("town", "Towns", "towns")}<div class="cats towns" role="group" aria-labelledby="lbl-towns">{tbuttons}</div></div>'
    return f'<div class="controls">{show}{theme}{cats}{towns}</div>'


def page_css():
    light, dark, classes = cat_css()
    return (CSS.replace("ACCENT_LIGHT", SITE["accent"]["light"]).replace("ACCENT_DARK", SITE["accent"]["dark"])
            .replace("CAT_LIGHT", light).replace("CAT_DARK", dark).replace("CAT_CLASSES", classes))


def head(places):
    css = page_css()
    gc = SITE.get("goatcounter") if PUBLIC else None
    tag = f'<script data-goatcounter="https://{esc(gc)}.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script>\n' if gc else ""
    # Favicon: Kauai's outline in purple, the island's official color. The PNG serves iPhone home screens.
    icon = (f'<link rel="icon" type="image/svg+xml" href="{esc(SITE["favicon"])}">\n'
            '<link rel="apple-touch-icon" href="apple-touch-icon.png">\n') if SITE.get("favicon") else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="darkreader-lock">
{'<meta name="robots" content="noindex, nofollow">' if SITE.get("noindex") or not PUBLIC else ""}
<meta name="theme-color" content="#f4f3ef" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#15181d" media="(prefers-color-scheme: dark)">
<title>{esc(SITE["title"])}</title>
<meta name="description" content="{esc(SITE["description"])}">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:ital,wght@0,400;1,400&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
{icon}{tag}<style>{css}</style></head><body><main>
<div class="top"><div><h1>{esc(SITE["title"])}</h1><p class="sub">{esc(SITE["intro"])}</p></div>{controls(places)}</div>
"""


def footer(memories_path):
    zone = SITE["timezones"][0]["label"]
    edited = datetime.fromtimestamp(memories_path.stat().st_mtime, timezone.utc)
    return (f'<footer>Built {esc(stamp(NOW))} {esc(zone)}. Memories last edited {esc(stamp(edited))} {esc(zone)}. Background imagery: Esri, Maxar, Earthstar Geographics.</footer></main>'
            f'<script>{THEME_JS.replace("SITE_KEY", SITE["slug"])}</script>')


# ---------------------------------------------------------------------------
# Sections.
# ---------------------------------------------------------------------------

def taken_text(exif_time):
    """EXIF "2016:04:13 10:22:31" (camera local time) as "April 13, 2016, 10:22 AM"."""
    if not exif_time:
        return "Unknown"
    try:
        return datetime.strptime(exif_time[:19], "%Y:%m:%d %H:%M:%S").strftime("%B %-d, %Y, %-I:%M %p")
    except ValueError:
        return exif_time


def meters(a_lat, a_lng, b_lat, b_lng):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lng - a_lng) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def attach_photos(places):
    """Add each place's photos from photos.json, oldest first. Returns ({photo ID: viewer data}, loose),
    where loose lists the photos with no place yet, oldest first, for the "Photos with no place yet" section."""
    if not PHOTOS_JSON.exists():
        return {}, []
    by_id = {p["id"]: p for p in places}
    albums = SITE.get("albums", {})
    viewer, loose = {}, []
    for pid, ph in sorted(json.loads(PHOTOS_JSON.read_text())["photos"].items(), key=lambda kv: (kv[1].get("taken") or "", kv[0])):
        place = by_id.get(ph.get("place"))
        if ph.get("hidden") or (ph.get("place") and not place):
            continue
        stem = Path(ph["file"]).stem
        thumb, full = f"photos/thumb/{ph['album']}/{stem}.jpg", f"photos/full/{ph['album']}/{stem}.jpg"
        if not ((BASE / SITE["output"]).parent / full).exists():
            continue
        if not place:
            info = [["Photo ID", pid]] + ([["Title", ph["title"]]] if ph.get("title") else []) + [["Place", "No place yet"], ["Taken", taken_text(ph.get("taken"))],
                    ["Album", albums.get(ph["album"], ph["album"])], ["Original file", ph["file"]]]
            if ph.get("camera"):
                info.append(["Camera", ph["camera"]])
            maps = None
            if "lat" in ph and not PUBLIC:
                info.append(["Location", f'{ph["lat"]:.5f}, {ph["lng"]:.5f}'])
                maps = f'https://www.google.com/maps/search/?api=1&query={ph["lat"]},{ph["lng"]}'
            loose.append({"pid": pid, "thumb": thumb, "album": ph["album"], "title": ph.get("title")})
            viewer[pid] = {"full": full, "cap": f'{pid}, {ph.get("title") or taken_text(ph.get("taken")).rsplit(",", 1)[0]}', "info": info, "maps": maps}
            continue
        place.setdefault("photos", []).append({"pid": pid, "thumb": thumb})
        year = (ph.get("taken") or "")[:4] or (re.findall(r"\b(?:19|20)\d\d\b", albums.get(ph["album"], "")) or [""])[0]
        if year:
            place.setdefault("photo_years", set()).add(year)
        info = [["Photo ID", pid]] + ([["Title", ph["title"]]] if ph.get("title") else []) + [["Place", place["name"]], ["Taken", taken_text(ph.get("taken"))],
                ["Album", albums.get(ph["album"], ph["album"])], ["Original file", ph["file"]]]
        if ph.get("camera"):
            info.append(["Camera", ph["camera"]])
        maps = None
        if "lat" in ph and not PUBLIC:
            dist = meters(ph["lat"], ph["lng"], place["lat"], place["lng"])
            info.append(["Location", f'{ph["lat"]:.5f}, {ph["lng"]:.5f}'])
            info.append(["Distance from pin", f"{dist:,.0f} m"])
            maps = f'https://www.google.com/maps/search/?api=1&query={ph["lat"]},{ph["lng"]}'
        viewer[pid] = {"full": full, "cap": f'{pid}, {ph.get("title") or taken_text(ph.get("taken")).rsplit(",", 1)[0]}', "info": info, "maps": maps}
    return viewer, loose


def years_text(p):
    """The memory's own `years`, or else the years its photos were taken (an undated photo counts as its album's year)."""
    return p.get("years") or ", ".join(sorted(p.get("photo_years", ())))


def status_text(p):
    if p["status"] == "unknown":
        return "Not known yet"
    if p["status"] == "open":
        return CATEGORIES[p["category"]]["open_label"]
    return f"Closed {p['closed_year']}" if p.get("closed_year") else "Closed"


def quotes(escaped):
    """Render "double-quoted" words in a memory as a <q> quotation, so quoted
    speech or song keeps its own punctuation and the page checker skips it."""
    return re.sub(r"&quot;(.+?)&quot;", r"<q>\1</q>", escaped)


def memory_card(p):
    cat = CATEGORIES[p["category"]]
    closed = " closed" if p["status"] == "closed" else ""
    chips = (f'<div class="chips"><span class="chip"><span class="dot cat-{esc(cat["id"])}{closed}"></span>{esc(cat["label"])}</span>'
             f'{"" if p["status"] == "unknown" else f"<span class={chr(34)}chip{chr(34)}>{esc(status_text(p))}</span>"}</div>')
    facts = []
    if p.get("years"):
        facts.append(("Years we went", p["years"]))
    elif p.get("photo_years"):
        facts.append(("Photos from", years_text(p)))
    if p.get("now") and not p.get("history"):
        facts.append(("There now", p["now"]))
    dl = f'<dl class="facts">{"".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts)}</dl>' if facts else ""
    history = ""
    if p.get("history"):
        steps = [esc(h) for h in p["history"]] + ([f'Today: {esc(p["now"])}'] if p.get("now") else [])
        history = f'<p class="histlabel small">What has stood here</p><ol class="history">{"".join(f"<li>{s}</li>" for s in steps)}</ol>'
    story = "".join(f"<p>{quotes(esc(t))}</p>" for t in p["paragraphs"])
    notes = []
    shots = p.get("photos", [])
    if shots:
        thumbs = "".join(f'<button type="button" class="thumb" data-pid="{esc(ph["pid"])}" aria-label="Open photo {esc(ph["pid"])}">'
                         f'<img src="{esc(ph["thumb"])}" alt="" loading="lazy"></button>' for ph in shots)
        notes.append(f'<p class="histlabel small">Photos ({len(shots)})</p><div class="thumbs">{thumbs}</div>')
    if p.get("approximate"):
        notes.append('<p class="small">The pin marks the general area.</p>')
    if p["status"] == "open" and cat["maps"] != "none":
        query = f'{p["name"]}, {p["town"]}, Kauai' if cat["maps"] == "name" else f'{p["lat"]},{p["lng"]}'
        notes.append(f'<p class="small">{link("https://www.google.com/maps/search/?api=1&query=" + quote_plus(query), "Open in Google Maps")}</p>')
    # "Eleele Shopping Center" already names its town; "Keokeo Road" needs "Eleele, " in front.
    spot = p.get("spot", "")
    where = spot if spot.startswith(p["town"]) else f'{p["town"]}, {spot}' if spot else p["town"]
    return (f'<article class="memory" id="m-{esc(p["id"])}" data-id="{esc(p["id"])}" hidden>'
            f'<h3>{esc(p["name"])}</h3><p class="where">{esc(where)}</p>{chips}{dl}'
            f'{history}<div class="story">{story}</div>{"".join(notes)}</article>')


VIEWER = ('<dialog id="viewer" aria-label="Photo viewer"><div class="flip" id="v-flip">'
          '<div class="face front"><img id="v-img" alt=""><button type="button" class="info" id="v-info" aria-label="Photo details" aria-pressed="false">i</button></div>'
          '<div class="face back"><dl class="facts" id="v-meta"></dl><p class="small" id="v-maps"></p>'
          '<button type="button" class="info back-btn" id="v-front" aria-label="Back to photo">&#x2715;</button></div></div>'
          '<p class="small vcap" id="v-cap"></p><div class="pager"><button type="button" id="v-prev">Previous</button>'
          '<button type="button" id="v-close">Close</button><button type="button" id="v-next">Next</button></div></dialog>')


def explore(places):
    if places:
        prompt = '<p class="muted" id="memory-empty">Tap a pin to read its memory.</p>'
        pager = ('<div class="pager"><button type="button" id="prev">Previous</button>'
                 '<span class="small" id="pos"></span><button type="button" id="next">Next</button></div>')
    else:
        prompt = '<p class="muted" id="memory-empty">No places on the map yet.</p>'
        pager = ""
    cards = "".join(memory_card(p) for p in places)
    legend = ('<p class="legend small"><span><span class="dot"></span>Filled pin: still here</span>'
              '<span><span class="dot closed"></span>Hollow pin: closed</span>'
              + ('<span><span class="dot dashed"></span>Dashed pin: not named yet</span>' if any(p["category"] == "unnamed" for p in places) else "")
              + '</p>') if places else ""
    return (f'{legend}<div class="explore"><div id="map" role="region" aria-label="Map of Kauai"></div>'
            f'<aside class="panel" id="panel" aria-live="polite">{pager}{prompt}{cards}</aside></div>{VIEWER}')


def place_list(places):
    rows, attrs = [], []
    for p in places:
        cat = CATEGORIES[p["category"]]
        rows.append([f'<a href="#{esc(p["id"])}" class="go" data-id="{esc(p["id"])}">{esc(p["name"])}</a>',
                     esc(p["town"]), esc(cat["label"]), esc(years_text(p)), esc(status_text(p))])
        attrs.append(f'data-id="{esc(p["id"])}"')
    intro = '<p class="small">Sorted by name. Years come from the memory, or from the photos when the memory gives none. Select a column header to sort by it, or a name to open its memory on the map.</p>'
    return twisty("All places", intro + table(["Place", "Town", "Category", "Years", "Status"], rows, row_attrs=attrs),
                  count=len(places), id_="places")


def loose_photos(loose):
    """Photos with no place yet (most have no GPS), by album, each labeled with its photo ID."""
    if not loose:
        return ""
    albums = SITE.get("albums", {})
    by_album = {}
    for ph in sorted(loose, key=lambda ph: ph["pid"]):
        by_album.setdefault(ph["album"], []).append(
            f'<figure class="loose"><button type="button" class="thumb" data-pid="{esc(ph["pid"])}" aria-label="Open photo {esc(ph["pid"])}">'
            f'<img src="{esc(ph["thumb"])}" alt="" loading="lazy"></button><figcaption>{esc(ph["pid"])}{f'<span class="ttl">{esc(ph["title"])}</span>' if ph.get("title") else ""}</figcaption></figure>')
    parts = "".join(f'<h3>{esc(albums.get(a, a))} <span class="count">{len(figs)}</span></h3><div class="thumbs wide">{"".join(figs)}</div>'
                    for a, figs in sorted(by_album.items()))
    intro = ('<p class="small">These photos have no location data, so they have no pin yet. '
             'Select a photo to view it, then <strong>i</strong> for its details.</p>')
    return twisty("Photos with no place yet", intro + parts, count=len(loose), open_=False, id_="loose")


def sections(places, loose=()):
    out = [explore(places)]
    if places:
        out.append(place_list(places))
    out.append(loose_photos(loose))
    return "\n".join(out)


def scripts(places, viewer):
    data = json.dumps([{k: p[k] for k in ("id", "name", "town", "category", "status", "lat", "lng")} for p in places])
    data = data.replace("</", "<\\/")
    return ('<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>'
            f'<script type="application/json" id="places-data">{data}</script>'
            f'<script type="application/json" id="photos-data">{json.dumps(viewer, ensure_ascii=False).replace("</", "<\\/")}</script>'
            f'<script>{MAP_JS.replace("SITE_KEY", SITE["slug"])}</script></body></html>')


def main():
    """Write the public page (docs/index.html, no photo GPS) and the private page
    (docs/private.html, every detail; git ignores it, so it never leaves this Mac)."""
    global PUBLIC
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--memories", type=Path, default=BASE / SITE["memories"])
    ap.add_argument("--out", type=Path, default=BASE / SITE["output"])
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for public, out in ((True, args.out), (False, args.out.with_name("private.html"))):
        PUBLIC = public
        places = load_places(args.memories)
        viewer, loose = attach_photos(places)
        page = head(places) + sections(places, loose) + footer(args.memories) + scripts(places, viewer)
        out.write_text(page)
        print(f"wrote {out} ({len(page)} bytes, {len(places)} places, {len(viewer) - len(loose)} photos on pins, {len(loose)} with no place)")
    if SITE.get("noindex"):
        (args.out.parent / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    # Photo copies the public page does not show (hidden or duplicate photos) stay on disk but out of the repo.
    root = args.out.parent
    shown = set(re.findall(r'photos/(?:thumb|full)/[^"\\]+?\.jpg', args.out.read_text()))
    unused = sorted(str(f.relative_to(root)) for f in (root / "photos").rglob("*.jpg") if str(f.relative_to(root)) not in shown)
    (root / ".gitignore").write_text("# Written by build_site.py: photo copies the public page does not show.\n"
                                    + "".join(f"/{u}\n" for u in unused))
    print(f"{len(shown)} photo files published, {len(unused)} kept local")


if __name__ == "__main__":
    main()
