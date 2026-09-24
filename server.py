#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bons Plans Domadoo – Domotique Technoseb27
Petit serveur local : analyse la page Promotions de Domadoo et sert l'interface
sur http://127.0.0.1:8765. Aucune dépendance : bibliothèque standard + curl de macOS.
"""
import hashlib, html, json, math, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, urllib.request, webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = os.environ.get("DOMADOO_BASE", "https://www.domadoo.fr")
LIST_PATH = "/fr/promotions"
PORT = int(os.environ.get("BPD_PORT", "8765"))
MAX_PAGES = 25
CACHE_SECONDS = 600          # une nouvelle analyse au plus toutes les 10 min (sauf « forcer »)
IDLE_EXIT_SECONDS = 900      # arrêt automatique 15 min après la fermeture de la page
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = os.environ.get("BPD_VERSION", "dev")
BOOT = os.environ.get("BPD_BOOT", "")
LOG_DIR = os.path.join(os.path.expanduser("~"), "Library", "Logs", "BonsPlansDomadoo")

_cache = {"at": 0, "data": None}
_lock = threading.Lock()
_last_seen = time.time()


def log(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "journal.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def save_debug(name, text):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


# ---------------------------------------------------------------- téléchargement
def fetch(url, want_json=False):
    headers = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"}
    if want_json:
        headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        headers["X-Requested-With"] = "XMLHttpRequest"
    else:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    curl = shutil.which("curl") or ("/usr/bin/curl" if os.path.exists("/usr/bin/curl") else None)
    if curl:  # curl de macOS utilise le trousseau système : pas de souci de certificats SSL
        cmd = [curl, "-sSL", "--compressed", "--max-time", "40", "-w", "\n__HTTP_CODE__%{http_code}"]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            out = r.stdout.decode("utf-8", "replace")
            body, _, code = out.rpartition("\n__HTTP_CODE__")
            if code.strip() not in ("200", ""):
                log(f"HTTP {code.strip()} sur {url}")
            return body
        log(f"curl a échoué ({r.returncode}) sur {url} : {r.stderr.decode('utf-8', 'replace').strip()}")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- utilitaires
def strip_tags(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = html.unescape(str(v)).replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    n = m.group(0)
    if "," in n and "." in n:
        n = n.replace(".", "").replace(",", ".") if n.rfind(",") > n.rfind(".") else n.replace(",", "")
    elif "," in n:
        n = n.replace(",", ".")
    try:
        return float(n)
    except ValueError:
        return None


def stock_label(text):
    for lab in ("Derniers articles", "Réapprovisionnement", "Sur commande", "En stock", "Rupture", "Épuisé"):
        if lab.lower() in (text or "").lower():
            return lab
    return ""


def finish(p):
    """Complète et valide un produit ; renvoie None s'il n'est pas vraiment en promo."""
    if not p.get("url") or not p.get("title") or not p.get("price") or not p.get("old"):
        return None
    if p["price"] >= p["old"]:
        return None
    if not p.get("pct"):
        p["pct"] = round((1 - p["price"] / p["old"]) * 100)
    url = p["url"]
    if url.startswith("/"):
        url = BASE + url
    p["url"] = url.split("#")[0]
    for k in ("img", "img_large"):
        if p.get(k, "").startswith("/"):
            p[k] = BASE + p[k]
    if not p.get("id"):
        m = re.search(r"/(\d+)-[^/]+\.html", p["url"])
        p["id"] = m.group(1) if m else p["url"]
    p["id"] = str(p["id"])
    if not p.get("brand"):
        t = re.split(r"\s+[-–]\s+", p["title"], maxsplit=1)[0]
        p["brand"] = t.split()[0].title() if t else ""
    p.setdefault("desc", "")
    p.setdefault("stock", "")
    p.setdefault("img", "")
    p.setdefault("img_large", "")
    return p


# ---------------------------------------------------------------- analyse JSON (PrestaShop)
def parse_json(text):
    try:
        d = json.loads(text)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    prods = d.get("products")
    out = []
    if isinstance(prods, list) and prods:
        for p in prods:
            if not isinstance(p, dict):
                continue
            cover = p.get("cover") or p.get("default_image") or {}
            by = cover.get("bySize", {}) if isinstance(cover, dict) else {}
            img = ((by.get("home_default") or {}).get("url")
                   or (cover.get("medium") or {}).get("url") if isinstance(cover, dict) else "") or ""
            img_large = ((cover.get("large") or {}).get("url") if isinstance(cover, dict) else "") or \
                        (by.get("large_default") or {}).get("url") or ""
            pct = p.get("discount_percentage_absolute") or p.get("discount_percentage") or ""
            pct = int(parse_price(pct) or 0) if pct else 0
            item = finish({
                "id": p.get("id_product") or p.get("id"),
                "title": strip_tags(p.get("name", "")),
                "url": p.get("url") or p.get("link") or "",
                "img": img, "img_large": img_large,
                "price": parse_price(p.get("price_amount")) or parse_price(p.get("price")),
                "old": parse_price(p.get("regular_price_amount")) or parse_price(p.get("regular_price")),
                "pct": abs(pct),
                "desc": strip_tags(p.get("description_short", "")),
                "brand": strip_tags(p.get("manufacturer_name") or ""),
                "ref": p.get("reference") or "",
                "stock": stock_label(p.get("availability_message") or "") or (p.get("availability_message") or ""),
            })
            if item:
                out.append(item)
    elif isinstance(d.get("rendered_products"), str):
        out = parse_html(d["rendered_products"])
    pages = None
    pag = d.get("pagination")
    if isinstance(pag, dict):
        pages = pag.get("pages_count")
    return {"products": out, "pages": pages}


# ---------------------------------------------------------------- analyse HTML (secours)
ART_RE = re.compile(r"(<article\b[^>]*product-miniature[^>]*>)(.*?)</article>", re.S | re.I)
EURO_RE = re.compile(r"(\d{1,3}(?:[\s\u00a0\u202f.]\d{3})*,\d{2})\s*(?:&nbsp;|\u00a0|\u202f|\s)*€")


def parse_block(open_tag, b):
    p = {}
    m = re.search(r'data-id-product="(\d+)"', open_tag + b)
    if m:
        p["id"] = m.group(1)
    links = re.findall(r'<a\b[^>]*href="([^"#]+\.html)[^"]*"[^>]*>(.*?)</a>', b, re.S | re.I)
    if links:
        p["url"] = html.unescape(links[0][0])
    m = re.search(r'<h\d[^>]*product-title[^>]*>.*?<a[^>]*>(.*?)</a>', b, re.S | re.I)
    if m:
        p["title"] = strip_tags(m.group(1))
    if not p.get("title"):
        m = re.search(r'<a[^>]*title="([^"]{10,})"[^>]*href="[^"]+\.html', b)
        if m:
            p["title"] = html.unescape(m.group(1))
    imgs = re.findall(r'<img\b[^>]*>', b, re.I)
    for tag in imgs:
        for attr in ("data-full-size-image-url", "data-src", "src", "data-lazy-src"):
            mm = re.search(attr + r'="([^"]+\.(?:jpe?g|png|webp)[^"]*)"', tag, re.I)
            if mm:
                src = html.unescape(mm.group(1))
                if attr == "data-full-size-image-url":
                    p.setdefault("img_large", src)
                elif not p.get("img"):
                    p["img"] = src
        if not p.get("title"):
            mm = re.search(r'alt="([^"]{10,})"', tag)
            if mm:
                p["title"] = html.unescape(mm.group(1))
    m = re.search(r'itemprop="price"[^>]*content="([\d.]+)"', b) or re.search(r'content="([\d.]+)"[^>]*itemprop="price"', b)
    if m:
        p["price"] = float(m.group(1))
    m = re.search(r'class="[^"]*regular-price[^"]*"[^>]*>(.*?)<', b, re.S)
    if m:
        p["old"] = parse_price(m.group(1))
    if not p.get("price"):
        m = re.search(r'class="(?:[^"]*\s)?price(?:\s[^"]*)?"[^>]*>(.*?)<', b, re.S)
        if m:
            p["price"] = parse_price(m.group(1))
    euros = [parse_price(x) for x in EURO_RE.findall(b)]
    euros = [x for x in euros if x]
    if len(euros) >= 2:
        p.setdefault("old", max(euros))
        p.setdefault("price", min(euros))
    m = re.search(r'-\s?(\d{1,2})\s?%', b)
    if m:
        p["pct"] = int(m.group(1))
    m = re.search(r'class="[^"]*description-short[^"]*"[^>]*>(.*?)</(?:div|p|a|span)>', b, re.S | re.I)
    if m:
        p["desc"] = strip_tags(m.group(1))
    if not p.get("desc"):
        for _, inner in links:
            t = strip_tags(inner)
            if len(t) > 40 and t != p.get("title") and not t.startswith("http"):
                p["desc"] = t
                break
    m = re.search(r'class="[^"]*(?:product-manufacturer|manufacturer-name|product-brand)[^"]*"[^>]*>(.*?)</', b, re.S | re.I)
    if m:
        p["brand"] = strip_tags(m.group(1))
    p["stock"] = stock_label(strip_tags(b))
    return finish(p)


def parse_html(text):
    out = []
    blocks = ART_RE.findall(text)
    if not blocks:  # structure différente : on découpe sur les vignettes produit
        parts = re.split(r'(?=<(?:div|li)\b[^>]*class="[^"]*(?:product-miniature|js-product-miniature|product-container)[^"]*")', text)
        blocks = [("", x) for x in parts[1:]]
    for open_tag, b in blocks:
        item = parse_block(open_tag, b)
        if item:
            out.append(item)
    return out


PROD_URL_RE = re.compile(r'href="((?:https?://[^"/]*domadoo\.fr)?/[^"]*?/(\d+)-[^"/]+\.html)[^"]*"', re.I)
TRIPLE_RE = re.compile(r"(\d{1,3}(?:[\s.]\d{3})*,\d{2})\s*€\s*-\s*(\d{1,2})\s*%\s*(\d{1,3}(?:[\s.]\d{3})*,\d{2})\s*€")


def parse_generic(text):
    """Analyse indépendante de la mise en page : découpe la page à chaque nouveau lien produit
    et cherche le motif « ancien prix € -N% nouveau prix € » dans chaque segment."""
    segs, last_pid = [], None
    for m in PROD_URL_RE.finditer(text):
        pid = m.group(2)
        if pid != last_pid:
            segs.append([m.start(), pid, html.unescape(m.group(1))])
            last_pid = pid
    found = {}
    for i, (start, pid, url) in enumerate(segs):
        chunk_start = text.rfind("<", 0, start)
        end = segs[i + 1][0] if i + 1 < len(segs) else min(len(text), start + 8000)
        end = text.rfind("<", 0, end) if i + 1 < len(segs) else end
        chunk = text[chunk_start:end]
        t = strip_tags(chunk)
        m = TRIPLE_RE.search(t)
        if m:
            old, pct, new = parse_price(m.group(1)), int(m.group(2)), parse_price(m.group(3))
        else:
            euros = [parse_price(x) for x in EURO_RE.findall(chunk)]
            euros = [x for x in euros if x]
            mp = re.search(r"-\s?(\d{1,2})\s?%", t)
            if len(euros) < 2 or not mp:
                continue
            old, new, pct = max(euros), min(euros), int(mp.group(1))
        p = {"id": pid, "url": url, "old": old, "price": new, "pct": pct}
        titles = [html.unescape(x) for x in re.findall(r'\btitle="([^"]{10,200})"', chunk)]
        alts = [html.unescape(x) for x in re.findall(r'\balt="([^"]{10,200})"', chunk)]
        heads = [strip_tags(x) for x in re.findall(r"<h\d[^>]*>(.*?)</h\d>", chunk, re.S)]
        link_txt = [strip_tags(x) for x in re.findall(r"<a\b[^>]*>(.*?)</a>", chunk, re.S)]
        link_txt = [x for x in link_txt if 10 <= len(x) <= 200 and not x.startswith("http")]
        cands = [x for x in heads if len(x) >= 10] + titles + alts
        p["title"] = cands[0] if cands else (min(link_txt, key=len) if link_txt else "")
        for tag in re.findall(r"<img\b[^>]*>", chunk, re.I):
            mm = re.search(r'(?:data-src|data-lazy-src|src)="([^"]+\.(?:jpe?g|png|webp)[^"]*)"', tag, re.I)
            if mm:
                p["img"] = html.unescape(mm.group(1))
                break
        descs = [x for x in link_txt if len(x) > 40 and x[:15].lower() != p["title"][:15].lower()]
        if descs:
            p["desc"] = descs[0]
        p["stock"] = stock_label(t)
        item = finish(p)
        if item and pid not in found:
            found[pid] = item
    return list(found.values())


ANTIBOT = ("cf-browser-verification", "challenge-platform", "cf-chl-", "just a moment...", "attention required! | cloudflare",
           "datadome", "px-captcha", "pardon our interruption", "request unsuccessful", "access denied</title>")


def looks_blocked(text):
    low = (text or "")[:20000].lower()
    return any(k in low for k in ANTIBOT)


def html_page_count(text, per_page):
    m = re.search(r"Il y a\s+(\d+)\s+produit", text) or re.search(r"de\s+(\d+)\s+article", text)
    if m and per_page:
        return math.ceil(int(m.group(1)) / per_page)
    return None


# ---------------------------------------------------------------- analyse complète
def scan_direct():
    products, seen_ids = [], set()
    mode, pages = None, None
    n = 1
    while n <= MAX_PAGES:
        base_url = f"{BASE}{LIST_PATH}?page={n}"
        page_items = None
        if mode in (None, "json"):
            try:
                txt = fetch(base_url + "&from-xhr=1", want_json=True)
                if n == 1:
                    save_debug("derniere-reponse-json.txt", txt[:200000])
                res = parse_json(txt)
                if res and res["products"]:
                    mode, page_items = "json", res["products"]
                    pages = pages or res["pages"]
                elif mode == "json":
                    page_items = []
            except Exception as e:  # noqa
                log(f"JSON page {n} : {e}")
        if page_items is None:
            txt = fetch(base_url)
            page_items = parse_html(txt)
            page_mode = "html"
            if not page_items:
                page_items = parse_generic(txt)
                page_mode = "generique"
            if page_items:
                mode = mode or page_mode
                pages = pages or html_page_count(txt, len(page_items))
            elif n == 1:
                save_debug("derniere-page.html", txt)
                if looks_blocked(txt):
                    raise RuntimeError("Domadoo a bloqué la lecture automatique (protection anti-robots). "
                                       f"La page reçue a été enregistrée dans {LOG_DIR}/derniere-page.html")
                raise RuntimeError("Aucun produit reconnu sur la page Promotions. "
                                   f"La page reçue a été enregistrée dans {LOG_DIR}/derniere-page.html")
        new = [p for p in page_items if p["id"] not in seen_ids]
        if not new:
            break
        for p in new:
            seen_ids.add(p["id"])
        products.extend(new)
        if pages and n >= pages:
            break
        n += 1
        time.sleep(0.6)  # on reste courtois avec le serveur de Domadoo
    log(f"Analyse terminée : {len(products)} produits, {n} page(s), mode {mode}")
    return {"ok": True, "products": products, "pages": n, "mode": mode,
            "scanned_at": datetime.now().strftime("%d/%m/%Y %H:%M")}


# ---------------------------------------------------------------- lecture via Safari (secours anti-robots)
class SafariError(RuntimeError):
    pass


JS_HELP = ("Safari doit autoriser le JavaScript envoyé par l'application. Dans Safari : menu Safari > Réglages > "
           "Avancées, cochez « Afficher les fonctionnalités pour les développeurs web ». Puis, selon votre version : "
           "Réglages > onglet « Développeur », cochez « Autoriser JavaScript depuis les Apple Events » ; ou, sur les "
           "anciennes versions, menu Développement > « Autoriser JavaScript depuis les Apple Events ». "
           "Cliquez ensuite à nouveau sur « Rafraîchir ».")


def osa(lines, *args, timeout=40):
    cmd = ["/usr/bin/osascript"]
    for ln in lines:
        cmd += ["-e", ln]
    cmd += list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        low = err.lower()
        log(f"osascript : {err}")
        if "-1743" in err or "not authorized" in low or "pas autoris" in low or "n’est pas autoris" in low:
            raise SafariError("macOS n'autorise pas encore l'application à piloter Safari. Ouvrez Réglages Système > "
                              "Confidentialité et sécurité > Automatisation et autorisez « Safari » pour Python / "
                              "Bons Plans Domadoo, puis cliquez à nouveau sur « Rafraîchir ».")
        if "apple event" in low and ("allow" in low or "autoris" in low):
            raise SafariError(JS_HELP)
        raise SafariError(f"Safari n'a pas pu lire la page : {err}")
    return r.stdout.decode("utf-8", "replace").rstrip("\n")


OSA_OPEN = ['on run argv', 'tell application "Safari"', 'make new document with properties {URL:(item 1 of argv)}',
            'return id of front window', 'end tell', 'end run']
OSA_NAV = ['on run argv', 'tell application "Safari" to set URL of current tab of window id ((item 1 of argv) as integer) to (item 2 of argv)', 'end run']
OSA_JS = ['on run argv', 'tell application "Safari" to return do JavaScript (item 2 of argv) in current tab of window id ((item 1 of argv) as integer)', 'end run']
OSA_CLOSE = ['on run argv', 'tell application "Safari" to close window id ((item 1 of argv) as integer)', 'end run']

JS_STATE = "document.readyState + '|' + document.querySelectorAll('a[href*=\".html\"]').length + '|' + location.href"
JS_HTML = ("(function(){var h=document.documentElement.outerHTML;"
           "return h.replace(/<script[\\s\\S]*?<\\/script>/gi,'').replace(/<style[\\s\\S]*?<\\/style>/gi,'')"
           ".replace(/<svg[\\s\\S]*?<\\/svg>/gi,'');})()")


def safari_wait(win, page, limit=75):
    t0 = time.time()
    while time.time() - t0 < limit:
        time.sleep(1.2)
        try:
            st = osa(OSA_JS, win, JS_STATE, timeout=20)
        except SafariError as e:
            if str(e) == JS_HELP or "Automatisation" in str(e):
                raise
            continue
        parts = st.split("|", 2)
        if len(parts) == 3 and parts[0] == "complete" and int(parts[1] or 0) > 8 and \
                (page == 1 or f"page={page}" in parts[2]):
            return True
    return False


def scan_safari():
    products, seen_ids, pages = [], set(), None
    win = osa(OSA_OPEN, f"{BASE}{LIST_PATH}?page=1")
    try:
        n = 1
        while n <= MAX_PAGES:
            if n > 1:
                osa(OSA_NAV, win, f"{BASE}{LIST_PATH}?page={n}")
            if not safari_wait(win, n):
                if n == 1:
                    raise SafariError("La page Promotions ne s'est pas chargée dans Safari (plus d'une minute). "
                                      "Si Domadoo affiche une vérification, validez-la dans la fenêtre Safari puis réessayez.")
                break
            txt = osa(OSA_JS, win, JS_HTML, timeout=60)
            items = parse_html(txt) or parse_generic(txt)
            if n == 1 and not items:
                save_debug("derniere-page.html", txt)
                raise SafariError("Safari a bien ouvert la page, mais aucun produit n'a été reconnu. "
                                  f"La page a été enregistrée dans {LOG_DIR}/derniere-page.html")
            pages = pages or html_page_count(txt, len(items))
            new = [p for p in items if p["id"] not in seen_ids]
            if not new:
                break
            for p in new:
                seen_ids.add(p["id"])
            products.extend(new)
            if pages and n >= pages:
                break
            n += 1
    finally:
        try:
            osa(OSA_CLOSE, win, timeout=10)
        except Exception:  # noqa
            pass
    log(f"Analyse via Safari terminée : {len(products)} produits, {n} page(s)")
    return {"ok": True, "products": products, "pages": n, "mode": "safari",
            "scanned_at": datetime.now().strftime("%d/%m/%Y %H:%M")}


# ---------------------------------------------------------------- lecture via le moteur WebKit de macOS (sans Safari)
SUPPORT_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "BonsPlansDomadoo")
WEBFETCH_SWIFT = r"""// webfetch – lecture des pages Promotions de Domadoo avec le moteur WebKit de macOS
// Usage : webfetch <url_de_base> <dossier_sortie> <pages_max>
import Cocoa
import WebKit

@MainActor
final class Box {
    var done = false
    var value: String? = nil
}

@MainActor
final class Loader: NSObject, WKNavigationDelegate {
    let web: WKWebView
    let window: NSWindow

    override init() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = WKWebsiteDataStore.default()
        cfg.applicationNameForUserAgent = "Version/17.5 Safari/605.1.15"
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 900), configuration: cfg)
        window = NSWindow(contentRect: NSRect(x: -12000, y: -12000, width: 1280, height: 900),
                          styleMask: [.borderless], backing: .buffered, defer: false)
        super.init()
        web.navigationDelegate = self
        window.contentView = web
        window.orderFrontRegardless()
    }

    func spin(_ seconds: Double) {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: seconds))
    }

    func eval(_ js: String, timeout: Double = 25) -> String? {
        let box = Box()
        web.evaluateJavaScript(js) { result, _ in
            if let s = result as? String { box.value = s }
            box.done = true
        }
        let end = Date(timeIntervalSinceNow: timeout)
        while !box.done && Date() < end { spin(0.05) }
        return box.value
    }

    func load(_ urlString: String) {
        if let url = URL(string: urlString) {
            web.load(URLRequest(url: url))
        }
    }
}

@main
struct WebFetch {
    @MainActor
    static func main() {
        let args = CommandLine.arguments
        guard args.count >= 4 else {
            print("usage: webfetch <url> <dossier> <pages_max>")
            exit(64)
        }
        let base = args[1]
        let outDir = args[2]
        let maxPages = Int(args[3]) ?? 25

        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let loader = Loader()

        let jsState = #"(function(){return document.readyState+'|'+document.querySelectorAll('a[href*=".html"]').length+'|'+location.href+'|'+document.title;})()"#
        let jsHtml = #"(function(){var h=document.documentElement.outerHTML;return h.replace(/<script[\s\S]*?<\/script>/gi,'').replace(/<style[\s\S]*?<\/style>/gi,'').replace(/<svg[\s\S]*?<\/svg>/gi,'');})()"#

        var page = 1
        var saved = 0
        while page <= maxPages {
            loader.load("\(base)?page=\(page)")
            loader.spin(1.0)
            let deadline = Date(timeIntervalSinceNow: 75)
            var ready = false
            while Date() < deadline {
                loader.spin(0.8)
                guard let st = loader.eval(jsState, timeout: 10) else { continue }
                let parts = st.components(separatedBy: "|")
                if parts.count >= 3, parts[0] == "complete", (Int(parts[1]) ?? 0) > 8,
                   page == 1 || parts[2].contains("page=\(page)") {
                    ready = true
                    break
                }
            }
            if !ready {
                if page == 1 {
                    let html = loader.eval(jsHtml, timeout: 30) ?? ""
                    try? html.write(toFile: "\(outDir)/page1-echec.html", atomically: true, encoding: .utf8)
                    print("ECHEC page 1")
                    exit(2)
                }
                break
            }
            loader.spin(1.0)
            guard let html = loader.eval(jsHtml, timeout: 40) else { break }
            try? html.write(toFile: "\(outDir)/page\(page).html", atomically: true, encoding: .utf8)
            saved += 1
            print("PAGE \(page) OK")
            let jsNext = "(function(){return String(Array.prototype.some.call(document.querySelectorAll('a[href]'),function(a){var m=a.href.match(/[?&]page=(\\d+)/);return m&&(+m[1])==\(page + 1);}));})()"
            if loader.eval(jsNext, timeout: 10) != "true" { break }
            page += 1
        }
        print("TERMINE \(saved)")
        exit(saved > 0 ? 0 : 2)
    }
}
"""


class WebKitError(RuntimeError):
    pass


def find_swiftc():
    for c in ("/usr/bin/swiftc", "/Library/Developer/CommandLineTools/usr/bin/swiftc"):
        if os.path.exists(c):
            return c
    try:
        r = subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], capture_output=True, timeout=20)
        p = r.stdout.decode().strip()
        if r.returncode == 0 and p:
            return p
    except Exception:  # noqa
        pass
    return None


def webfetch_binary():
    digest = hashlib.sha256(WEBFETCH_SWIFT.encode("utf-8")).hexdigest()[:12]
    bin_path = os.path.join(SUPPORT_DIR, f"webfetch-{digest}")
    if os.path.exists(bin_path):
        return bin_path
    swiftc = find_swiftc()
    if not swiftc:
        raise WebKitError("compilateur Swift introuvable")
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    src = os.path.join(SUPPORT_DIR, "webfetch.swift")
    with open(src, "w", encoding="utf-8") as f:
        f.write(WEBFETCH_SWIFT)
    log("Préparation du moteur WebKit (première utilisation)…")
    r = subprocess.run([swiftc, "-O", "-parse-as-library", "-o", bin_path + ".tmp", src],
                       capture_output=True, timeout=900)
    if r.returncode != 0:
        save_debug("compilation-webkit.txt", r.stderr.decode("utf-8", "replace"))
        raise WebKitError(f"compilation impossible (détails dans {LOG_DIR}/compilation-webkit.txt)")
    os.replace(bin_path + ".tmp", bin_path)
    for old in os.listdir(SUPPORT_DIR):  # ménage des anciennes versions
        if old.startswith("webfetch-") and os.path.join(SUPPORT_DIR, old) != bin_path:
            try:
                os.remove(os.path.join(SUPPORT_DIR, old))
            except OSError:
                pass
    return bin_path


def scan_webkit():
    binary = webfetch_binary()
    out = tempfile.mkdtemp(prefix="bpd-")
    try:
        r = subprocess.run([binary, f"{BASE}{LIST_PATH}", out, str(MAX_PAGES)], capture_output=True, timeout=600)
        log("WebKit : " + r.stdout.decode("utf-8", "replace").replace("\n", " / ").strip()
            + (" | " + r.stderr.decode("utf-8", "replace").strip()[-300:] if r.stderr else ""))
        files = sorted((f for f in os.listdir(out) if re.match(r"page\d+\.html$", f)),
                       key=lambda x: int(re.search(r"\d+", x).group(0)))
        if not files:
            fail = os.path.join(out, "page1-echec.html")
            if os.path.exists(fail):
                with open(fail, encoding="utf-8", errors="replace") as fh:
                    save_debug("derniere-page.html", fh.read())
            raise WebKitError("la page Promotions ne s'est pas chargée")
        products, seen_ids = [], set()
        for i, name in enumerate(files):
            with open(os.path.join(out, name), encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
            items = parse_html(txt) or parse_generic(txt)
            if i == 0 and not items:
                save_debug("derniere-page.html", txt)
                raise WebKitError("page chargée mais aucun produit reconnu")
            for p in items:
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    products.append(p)
        log(f"Analyse via WebKit terminée : {len(products)} produits, {len(files)} page(s)")
        return {"ok": True, "products": products, "pages": len(files), "mode": "webkit",
                "scanned_at": datetime.now().strftime("%d/%m/%Y %H:%M")}
    finally:
        shutil.rmtree(out, ignore_errors=True)


_preferred = None  # mémorise la méthode qui a fonctionné


def scan():
    global _preferred
    methods = [("direct", scan_direct)]
    if sys.platform == "darwin":
        methods += [("webkit", scan_webkit), ("safari", scan_safari)]
    if _preferred:
        methods.sort(key=lambda m: m[0] != _preferred)
    errors = []
    for name, fn in methods:
        try:
            res = fn()
            _preferred = name
            return res
        except Exception as e:  # noqa
            log(f"Méthode {name} impossible : {e}")
            errors.append((name, e))
    # message le plus utile : celui de la dernière méthode (Safari) s'il explique un réglage, sinon WebKit
    labels = {"direct": "Lecture directe", "webkit": "Moteur WebKit", "safari": "Safari"}
    raise RuntimeError(" · ".join(f"{labels[n]} : {e}" for n, e in errors))


def get_scan(force=False):
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return dict(_cache["data"], cached=True)
        data = scan()
        _cache.update(at=time.time(), data=data)
        return dict(data, cached=False)


# ---------------------------------------------------------------- serveur web local
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global _last_seen
        _last_seen = time.time()
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/ping":
            return self.send(200, json.dumps({"app": "bons-plans-domadoo"}))
        if u.path == "/api/scan":
            force = parse_qs(u.query).get("force", ["0"])[0] == "1"
            try:
                return self.send(200, json.dumps(get_scan(force), ensure_ascii=False))
            except Exception as e:
                log(f"Erreur d'analyse : {e}")
                return self.send(200, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        if u.path == "/api/version":
            return self.send(200, json.dumps({"version": VERSION}))
        if u.path == "/api/update-check":
            return self.send(200, json.dumps(run_boot("--check"), ensure_ascii=False))
        if u.path == "/api/update-install":
            res = run_boot("--update-only")
            self.send(200, json.dumps(res, ensure_ascii=False))
            if res.get("installed"):
                threading.Thread(target=restart, daemon=True).start()
            return
        if u.path == "/api/quit":
            self.send(200, json.dumps({"ok": True}))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.send(404, json.dumps({"error": "introuvable"}))


def run_boot(arg):
    if not BOOT or not os.path.exists(BOOT):
        return {"ok": False, "error": "Mises à jour indisponibles dans ce mode de lancement."}
    try:
        r = subprocess.run([sys.executable, BOOT, arg], capture_output=True, timeout=120)
        return json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)}


def restart():
    time.sleep(0.8)
    log("Redémarrage après mise à jour")
    env = dict(os.environ, BPD_NO_BROWSER="1")
    os.execve(sys.executable, [sys.executable, BOOT], env)


def watchdog(server):
    while True:
        time.sleep(30)
        if time.time() - _last_seen > IDLE_EXIT_SECONDS:
            log("Arrêt automatique (inactivité)")
            server.shutdown()
            return


def running_version():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/version", timeout=3) as r:
            return json.loads(r.read().decode()).get("version")
    except Exception:  # noqa
        return None


def free_port():
    """Arrête un ancien exemplaire bloqué ou d'une autre version. Renvoie True si le port a été libéré."""
    v = running_version()
    if v and v == VERSION:
        return False
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
                           capture_output=True, timeout=10)
        pids = [int(x) for x in r.stdout.decode().split() if x.strip().isdigit() and int(x) != os.getpid()]
    except Exception:  # noqa
        pids = []
    if not pids:
        return False
    log(f"Arrêt d'un ancien exemplaire (version {v or 'inconnue/bloquée'}, pid {pids})")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(1.5)
    return True


def detach():
    """Relance le serveur dans une session indépendante et rend la main tout de suite.
    macOS considère alors l'application comme terminée : un nouveau double-clic sur l'icône
    rouvre simplement la page au lieu de ne rien faire."""
    env = dict(os.environ, BPD_DETACHED="1")
    with open(os.devnull, "rb") as dn_in, open(os.devnull, "wb") as dn_out:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env, stdin=dn_in,
                         stdout=dn_out, stderr=dn_out, start_new_session=True, close_fds=True)


def main():
    if sys.platform == "darwin" and os.environ.get("BPD_DETACHED") != "1" and os.environ.get("BPD_NO_DETACH") != "1":
        detach()
        return
    server = None
    for attempt in range(2):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            if attempt == 0 and free_port():
                continue
            webbrowser.open(f"http://127.0.0.1:{PORT}/")  # la bonne version tourne déjà
            return
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if os.environ.get("BPD_NO_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    log("Démarrage")
    server.serve_forever()


if __name__ == "__main__":
    main()
