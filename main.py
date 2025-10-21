# -*- coding: utf-8 -*-
# İstinye ve Atlas Üniversiteleri öğrenim ücretlerini toplar ve arama yapar.
# İstinye: https://aday.istinye.edu.tr/taban-puan-ve-ucretler
# Atlas:   https://tercih.atlas.edu.tr/ogrenim-ucretleri

import csv
import re
import sys
import time
import unicodedata
import requests
import io
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse, unquote, parse_qs

ISTINYE_URL = "https://aday.istinye.edu.tr/taban-puan-ve-ucretler"
ATLAS_URL   = "https://tercih.atlas.edu.tr/ogrenim-ucretleri"

# Yeni: Arel PDF kaynağı
AREL_PDF_URL = "https://yataygecis.arel.edu.tr/wp-content/uploads/2025/07/istanbul-arel-universitesi-yatay-gecis-ucretleri-2025-2026.pdf"


# Eski:
# TL_RE = re.compile(r"(?:₺\s*([\d\.\,]+)|([\d\.\,]+)\s*(?:TL\b))", re.IGNORECASE)
# MONEY_IN_LINE_RE = re.compile(r"(\d[\d\.\,]*)\s*(?:TL|₺)?", re.IGNORECASE)

# Yeni: daha sağlam TL/birim yakalayıcıları
# Açıklama: binlik ayraçları nokta, boşluk veya NBSP olabilir; ondalık kısım virgülle gelebilir.
TL_RE = re.compile(
    r"(?:₺\s*)?([0-9]{1,3}(?:[.\s\u00A0][0-9]{3})*(?:,[0-9]+)?)\s*(?:TL\b)?",
    re.IGNORECASE
)
MONEY_IN_LINE_RE = re.compile(
    r"(?:₺\s*)?([0-9]{1,3}(?:[.\s\u00A0][0-9]{3})*(?:,[0-9]+)?)\s*(?:TL\b)?",
    re.IGNORECASE
)


def parse_money(cell_text: str):
    """
    '₺485.925' / '450.000 TL' / '450 000,50 TL' -> 485925 / 450000 (int)
    Bulamazsa None döner.
    Mantık: binlik ayraçlarını (. veya boşluk veya NBSP) temizle, varsa ondalık kısmı at.
    """
    if not cell_text:
        return None
    s = str(cell_text).replace("\u00A0", " ").strip()
    # önce TL_RE ile yakala, yoksa daha gevşek bir sayı yakala
    m = TL_RE.search(s)
    if m:
        raw = m.group(1)
    else:
        m2 = re.search(r"([0-9][0-9\.\s\u00A0,]*)", s)
        if not m2:
            return None
        raw = m2.group(1)

    # Temizleme: NBSP/boşluk/nokta binlik ayraçlarını kaldır, ondalık kısmı at
    cleaned = raw.replace("\u00A0", "").replace(" ", "").replace(".", "")
    if "," in cleaned:
        cleaned = cleaned.split(",", 1)[0]
    cleaned = re.sub(r"[^\d]", "", cleaned)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except Exception:
        return None

def nearest_faculty_title(table):
    """
    Tablo üstündeki en yakın başlığı (h1..h5/strong/p/caption) ipucu olarak fakülte adı say.
    Önce tabloyu saran caption/summary/parent içlerini kontrol eder, sonra önceki sibling/previous
    heading'leri ve atalarındaki başlıkları arar. Daha çok varyasyonu yakalamaya çalışır.
    """
    KEYWORDS = ["fakültesi", "fakulte", "fakülte", "yüksekokulu", "meslek yüksekokulu", "yüksekokul"]
    # 1) caption veya table summary
    cap = table.find("caption")
    if cap:
        txt = " ".join(cap.get_text(" ", strip=True).split())
        if any(k in txt for k in KEYWORDS):
            return txt

    # 2) yakın parent içindeki heading/p/strong'ları kontrol et (çevre 3 seviye)
    node = table
    for _ in range(6):
        parent = node.parent
        if not parent:
            break
        for tag in ["h1", "h2", "h3", "h4", "h5", "strong", "p"]:
            el = parent.find(tag)
            if el:
                txt = " ".join(el.get_text(" ", strip=True).split())
                if any(k in txt for k in KEYWORDS):
                    return txt
        node = parent

    # 3) önceki siblings ve önceki başlık elemanlarını daha derin tara
    node = table
    for _ in range(60):
        prev = node.find_previous()
        if not prev:
            break
        if prev.name and prev.name.lower() in ["h1","h2","h3","h4","h5","strong","p","div"]:
            txt = " ".join(prev.get_text(" ", strip=True).split())
            if any(k in txt for k in KEYWORDS):
                return txt
        node = prev

    # 4) fallback: herhangi bir yakın heading'ten kısa bir metin döndür (en yakın olan)
    node = table
    for _ in range(40):
        node = node.find_previous(["h1","h2","h3","h4","h5","strong","p"])
        if not node:
            break
        txt = " ".join(node.get_text(" ", strip=True).split())
        if len(txt) > 3:
            return txt

    return ""

def header_indexes(table):
    """
    Tablo başlıklarındaki sütunları tespit eder.
    Döndürdükleri (program_idx, pesin_idx, burslu_idx) olabilir; bulunamayanlar None.
    """
    ths = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
    if not ths:
        thead_tr = table.find("tr")
        if thead_tr:
            ths = [th.get_text(" ", strip=True).lower() for th in thead_tr.find_all(["th", "td"])]

    program_idx = None
    pesin_idx = None
    burslu_idx = None

    for i, h in enumerate(ths):
        if program_idx is None and any(k in h for k in ["program", "bölüm", "adı", "ad"]):
            program_idx = i
        if pesin_idx is None and ("peşin" in h or ("ücret" in h and burslu_idx is not None)):
            pesin_idx = i
        if burslu_idx is None and any(k in h for k in ["bilinçli", "burs", "burslu"]):
            burslu_idx = i

    # Yedek plan: "ücret" geçen ilk sütun peşin, ikincisi burslu
    if program_idx is None:
        program_idx = 0
    if pesin_idx is None or (pesin_idx < len(ths) and "ücret" not in ths[pesin_idx]):
        ucret_cols = [i for i, h in enumerate(ths) if "ücret" in h]
        if ucret_cols:
            pesin_idx = ucret_cols[0]
            if len(ucret_cols) > 1:
                burslu_idx = ucret_cols[1]

    return program_idx, pesin_idx, burslu_idx

def extract_table_rows(table, faculty_hint, university_name, source_url, default_year="2025"):
    """
    Bir tablodan satırları okuyup (program, peşin, burslu) döndürür.
    """
    rows = []
    body_rows = table.select("tbody tr") or table.select("tr")[1:]
    if not body_rows:
        return rows

    program_idx, pesin_idx, burslu_idx = header_indexes(table)

    for tr in body_rows:
        tds = tr.find_all(["td", "th"])
        if not tds or len(tds) < 2:
            continue
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]

        # Satırda TL var mı? (başlık/boş alt satırları ele)
        any_tl = any(("₺" in c) or (" TL" in c) for c in cells)
        if not any_tl:
            continue

        program = cells[program_idx] if program_idx < len(cells) else cells[0]
        pesin_val = parse_money(cells[pesin_idx]) if pesin_idx is not None and pesin_idx < len(cells) else None
        burslu_val = parse_money(cells[burslu_idx]) if burslu_idx is not None and burslu_idx < len(cells) else None

        # Olmadıysa ilk iki TL değerini sırayla al
        if pesin_val is None and burslu_val is None:
            money_vals = []
            for c in cells:
                val = parse_money(c)
                if val is not None:
                    money_vals.append(val)
            if money_vals:
                pesin_val = money_vals[0]
                if len(money_vals) > 1:
                    burslu_val = money_vals[1]

        if pesin_val is None and burslu_val is None:
            continue

        rows.append({
            "university": university_name,
            "faculty": faculty_hint,
            "program": program,
            "year": default_year,
            "fee_pesin_try": pesin_val,
            "fee_bilincli_burslu_try": burslu_val,
            "source": source_url
        })
    return rows

def scrape_generic(url, university_name, default_year="2025"):
    """
    Verilen URL'deki tüm tabloları gezip ücret satırlarını toplar.
    (İstinye tablo tabanlı olduğu için bu yeterli)
    """
    r = requests.get(url, headers={"User-Agent": "uni-fee-bot/1.0"}, timeout=30)
    r.raise_for_status()
    time.sleep(1)

    soup = BeautifulSoup(r.text, "html.parser")
    data = []
    for table in soup.find_all("table"):
        faculty = nearest_faculty_title(table)
        rows = extract_table_rows(table, faculty, university_name, url, default_year=default_year)
        if rows:
            data.extend(rows)
    return data

# -------- Atlas'a özel: tablo yerine akış (metin) üzerinden çekim --------

def iter_content_nodes(soup):
    """
    Atlas sayfası tablolu değil; metin akışındaki blokları sırayla dolaş.
    """
    for el in soup.find_all(["h1","h2","h3","strong","p","a","div"], recursive=True):
        txt = " ".join(el.get_text(" ", strip=True).split())
        yield el, txt

def extract_atlas_stream(soup):
    """
    Atlas sayfasında bölüm adları çoğunlukla <a> içinde ve aynı satırda '... TL' tutarları yer alıyor.
    Fakülte başlığını, akışta en son görülen '... FAKÜLTESİ' / '... YÜKSEKOKULU' metnine bağlarız.
    """
    rows = []
    current_faculty = ""

    for el, txt in iter_content_nodes(soup):
        if not txt:
            continue
        txt_up = txt.upper()

        # Fakülte başlığı ipuçları (büyük/küçük harf duyarsız)
        if ("FAKÜLTESİ" in txt_up) or ("YÜKSEKOKULU" in txt_up) or ("FAKULTE" in txt_up) or ("YÜKSEKOKUL" in txt_up):
            current_faculty = txt
            continue

        # Bölüm satırı: <a> içinde isim + aynı satırda TL'ler
        if el.name == "a" and ("TL" in txt_up or "₺" in txt):
            program = el.get_text(" ", strip=True)

            # Satırdaki TL değerlerini sırayla yakala ve None'ları filtrele
            nums = [v for v in (parse_money(m.group(0)) for m in MONEY_IN_LINE_RE.finditer(txt)) if v is not None]

            # Sık görülen sıra: [taksitli, tercih taksitli, PEŞİN, TERCIH+PEŞİN]
            pesin_val = nums[2] if len(nums) >= 3 else (nums[0] if nums else None)
            tercih_pesin_val = nums[3] if len(nums) >= 4 else (nums[1] if len(nums) > 1 else None)

            if pesin_val is None and tercih_pesin_val is None:
                continue

            rows.append({
                "university": "Atlas Üniversitesi",
                "faculty": current_faculty,
                "program": program,
                "year": "2025",
                "fee_pesin_try": pesin_val,
                "fee_bilincli_burslu_try": tercih_pesin_val,
                "source": ATLAS_URL
            })

    return rows

# Yeni yardımcı: PDF bytelarından metin çek (PyPDF2 varsa kullan)
def extract_text_from_pdf_bytes(data: bytes) -> str:
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for p in reader.pages:
            t = p.extract_text() or ""
            pages.append(t)
        return "\n".join(pages)
    except Exception:
        # Basit fallback: metni herhangi bir text decode ile al (PDF içeriği okunamazsa boş döner)
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            try:
                return data.decode("latin-1", errors="replace")
            except Exception:
                return ""

# Yeni: Arel (PDF) için scraping
def scrape_arel():
    """
    İstanbul Arel Üniversitesi ücret listesi (PDF) kaynağından basit metin tarama ile TL satırlarını çıkarır.
    Çok sağlam bir PDF parsing değil; PyPDF2 varsa daha iyi sonuç verir.
    """
    rows = []
    try:
        r = requests.get(AREL_PDF_URL, headers={"User-Agent": "uni-fee-bot/1.0"}, timeout=30)
        r.raise_for_status()
        time.sleep(1)
        text = extract_text_from_pdf_bytes(r.content)
    except Exception as e:
        print(f"[arel] PDF indirilemedi veya okunamadı: {e}")
        return rows

    # Satırlara ayır ve TL içeren satırları ara
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Basit heuristic: TL içeren satır program ismi + tutar içerebilir; değilse önceki satır program olabilir.
    for i, ln in enumerate(lines):
        if (" TL" in ln.upper()) or ("₺" in ln):
            # Satırdaki tüm eşleşmeleri tek seferde tarayıp değerleri topla
            money_vals = []
            for m in MONEY_IN_LINE_RE.finditer(ln):
                val = parse_money(m.group(0))
                if val is not None:
                    money_vals.append(val)
            if not money_vals:
                continue

            # Program adı tahmini: aynı satırda TL'den önce kalan metin ya da önceki satır
            # Para ifadelerini kaldırarak program adını temizle
            prog_candidate = MONEY_IN_LINE_RE.sub("", ln).strip(" -–:,.")
            if not prog_candidate:
                # önceki anlamlı satırı dene
                j = i - 1
                while j >= 0 and not lines[j].strip():
                    j -= 1
                if j >= 0:
                    prog_candidate = lines[j]
            # yine boşsa atla
            if not prog_candidate:
                continue

            # Peşin / burslu heuristiği: ilk iki para değeri sırayla al
            pesin_val = money_vals[0] if money_vals else None
            burslu_val = money_vals[1] if len(money_vals) > 1 else None

            rows.append({
                "university": "İstanbul Arel Üniversitesi",
                "faculty": "",  # PDF'den kolay ayrıştırılamıyor; boş bırak
                "program": prog_candidate,
                "year": "2025",
                "fee_pesin_try": pesin_val,
                "fee_bilincli_burslu_try": burslu_val,
                "source": AREL_PDF_URL
            })

    return rows

# ---------------- Yardımcılar: kayıtları kaydet / arama / yazdırma ----------------

def save_csv(rows, path):
    if not rows:
        print("Veri bulunamadı, CSV yazılmadı.")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"✓ Kaydedildi: {path}")

def normalize_text(s: str) -> str:
    s = s.lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s

def match_program(row_program: str, query: str) -> bool:
    rp = normalize_text(row_program)
    q  = normalize_text(query)
    return all(part in rp for part in q.split())

def find_fees(rows, query):
    return [r for r in rows if match_program(r["program"], query)]

def print_results(rows):
    if not rows:
        print("Sonuç yok.")
        return
    rows = sorted(rows, key=lambda r: (r["university"], r.get("faculty") or "", r["program"]))
    current_uni = None
    for r in rows:
        if r["university"] != current_uni:
            current_uni = r["university"]
            print(f"\n=== {current_uni} ===")
        prog = r["program"]
        fac  = f" ({r['faculty']})" if r.get("faculty") else ""
        pes  = f"{r['fee_pesin_try']:,} TL".replace(",", ".") if r.get("fee_pesin_try") else "-"
        bur  = f"{r['fee_bilincli_burslu_try']:,} TL".replace(",", ".") if r.get("fee_bilincli_burslu_try") else "-"
        print(f"- {prog}{fac} | Peşin: {pes} | Burslu/Bilinçli: {bur} | Kaynak: {r['source']}")


# python
def _resolve_ddg_link(href: str) -> str:
    """
    DuckDuckGo arama sonucu linkleri bazen '/l/?uddg=ENCODED_URL' formatında gelir.
    Bu fonksiyon varsa uddg parametresini çözer, yoksa href'i olduğu gibi döner.
    """
    if not href:
        return href
    # tam URL ise döndür
    if href.startswith("http://") or href.startswith("https://"):
        return href
    # örnek: /l/?kh=-1&uddg=https%3A%2F%2Faday.istinye.edu.tr%2F...
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    except Exception:
        pass
    # bazen DDG sonuçlarında gövde içinde tam URL yer alır
    if href.startswith("/l/") and "uddg=" in href:
        try:
            u = href.split("uddg=", 1)[1]
            return unquote(u)
        except Exception:
            pass
    # fallback: döndür
    return href

def generate_candidate_urls(name, max_per_variant=6):
    """
    Girilen üniversite adından türetilmiş olası domain ve path kombinasyonlarını üretir.
    Amaç, DDG sonuçları yetmezse doğrudan tahmini URL'leri denemek.
    """
    s = name.lower()
    s = s.replace("üniversitesi", "").replace("universitesi", "").strip()
    # normalize to ascii, keep letters/digits/space/hyphen
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", s)
    if not words:
        return []
    variants = []
    joined = "".join(words)
    hyphen = "-".join(words)
    underscore = "_".join(words)
    # temel domain varyasyonları
    base_domains = [
        f"https://{joined}.edu.tr",
        f"https://www.{joined}.edu.tr",
        f"https://{hyphen}.edu.tr",
        f"https://www.{hyphen}.edu.tr",
        f"https://{joined}.com",
        f"https://{hyphen}.com",
    ]
    # ortak path'ler
    paths = [
        "/ogrenim-ucretleri",
        "/ögrenim-ucretleri",  # yanlış yazılmışı da ekle
        "/öğrenim-ucretleri",
        "/ucretleri",
        "/ucret",
        "/taban-puan-ve-ucretler",
        "/ogrenim-ucretleri-2025",
        "/ogrenim-ucretleri/",
        "/ucretleri/",
        "/"
    ]
    for d in base_domains:
        for p in paths[:max_per_variant]:
            variants.append(d.rstrip("/") + p)
        variants.append(d)  # kök sayfa da denenmeli
    # ek: domain + "/tr" veya "/en"
    extra = []
    for d in base_domains:
        extra.append(d + "/tr")
        extra.append(d + "/en")
    variants.extend(extra)
    # temizle ve eşsizleştir, sıralı döndür
    seen = set()
    out = []
    for u in variants:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def search_university_url(name, max_results=12):
    """
    Basit davranış:
    - Google'a "name ücretleri" sorgusu gönder, ilk çıkan arama sonucunu al.
    - İlk sonucun domain'inde ortak ücret path'lerini dene; bulunursa döndür.
    - Google başarısız olursa DuckDuckGo fallback'ı ile benzer davranış uygula.
    """
    google = "https://www.google.com/search"
    ddg = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "uni-fee-bot/1.0 (compatible)",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    query = f"{name} ücretleri"
    verbose = True

    PATH_KEYWORDS = ["ucret", "ücret", "ogrenim", "öğrenim", "ucretleri", "taban-puan"]
    COMMON_PATHS = [
        "/ogrenim-ucretleri",
        "/öğrenim-ucretleri",
        "/ucretleri",
        "/ucret",
        "/taban-puan-ve-ucretler",
        "/burslar-ucretler",
        "/tr/ogrenim-ucretleri",
        "/tr/ucretleri",
        "/"
    ]

    def base_of(url):
        try:
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}"
        except Exception:
            return url

    def get_first_google_result(q):
        try:
            if verbose:
                print(f"[google] Aranıyor: {q}")
            r = requests.get(google, params={"q": q, "hl": "tr"}, headers=headers, timeout=7)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            # Google sonuçlarında /url?q=<target>&... biçimi sık kullanılır
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                # form: /url?q=https://... veya doğrudan https://...
                if href.startswith("/url?q="):
                    target = href.split("/url?q=", 1)[1].split("&", 1)[0]
                    if target.startswith("http"):
                        if verbose:
                            print(f"[google] İlk sonuç: {target}")
                        return target
                if href.startswith("http"):
                    if verbose:
                        print(f"[google] İlk sonuç (direkt): {href}")
                    return href
        except Exception as e:
            if verbose:
                print(f"[google] Hata veya timeout: {e}")
        return None

    def get_first_ddg_result(q):
        try:
            if verbose:
                print(f"[ddg] Aranıyor (fallback): {q}")
            r = requests.get(ddg, params={"q": q}, headers=headers, timeout=7)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                # çözümle /l/?uddg=...
                if href:
                    try:
                        resolved = _resolve_ddg_link(href)
                        if resolved and resolved.startswith("http"):
                            if verbose:
                                print(f"[ddg] İlk sonuç: {resolved}")
                            return resolved
                    except Exception:
                        continue
                    if href.startswith("http"):
                        if verbose:
                            print(f"[ddg] İlk sonuç (direkt): {href}")
                        return href
        except Exception as e:
            if verbose:
                print(f"[ddg] Hata veya timeout: {e}")
        return None

    def is_fee_page_simple(url):
        """Hızlı doğrulama: başlıklarda/ana içerikte 'ücret' ve 'tl/₺' arar."""
        try:
            if verbose:
                print(f"  [check] içerik kontrolü: {url}")
            r = requests.get(url, headers=headers, timeout=6)
            r.raise_for_status()
            text = r.text.lower()
            soup = BeautifulSoup(text, "html.parser")
            for h in soup.find_all(["h1", "h2", "h3"]):
                ht = " ".join(h.get_text(" ", strip=True).lower().split())
                if any(k in ht for k in PATH_KEYWORDS + ["ücret", "ücretleri"]):
                    return True
            main_sel = soup.find("main") or soup.find("article")
            if not main_sel:
                main_sel = soup.find("div", id=re.compile("(content|main|page|article)", re.I)) \
                           or soup.find("div", class_=re.compile("(content|main|page|article)", re.I))
            if main_sel:
                mtxt = " ".join(main_sel.get_text(" ", strip=True).lower().split())
                if ((" tl" in mtxt or "₺" in mtxt) and any(k in mtxt for k in ["ücret", "ucret", "öğrenim"])):
                    return True
            snippet = text[:12000]
            if ((" tl" in snippet or "₺" in snippet) and any(k in snippet for k in ["ücret", "ucret"])):
                return True
        except Exception:
            return False
        return False

    # 1) Google ile ilk sonucu al
    first = get_first_google_result(query)
    if not first:
        # Google başarısızsa DDG dene
        if verbose:
            print("[info] Google başarısız/davranışsız, DuckDuckGo ile denenecek.")
        first = get_first_ddg_result(query)
        if not first:
            if verbose:
                print("[fallback] Arama motorlarından sonuç alınamadı.")
            return None

    # 2) İlk sonucun domain'inde ortak path'leri dene
    base = base_of(first)
    for p in COMMON_PATHS:
        cand = base.rstrip("/") + p
        try:
            if is_fee_page_simple(cand):
                if verbose:
                    print(f"[found] Domain üzerinde ortak path bulundu: {cand}")
                return cand
        except Exception:
            continue

    # 3) İlk sonucu doğrudan doğrula; uygunsa döndür
    try:
        if is_fee_page_simple(first):
            if verbose:
                print(f"[found] İlk sonuç ücret sayfası gibi: {first}")
            return first
    except Exception:
        pass

    # 4) Hiçbir özel sayfa bulunamadıysa ilk sonucu döndür
    if verbose:
        print(f"[fallback] Özel ücret sayfası bulunamadı, ilk sonuç döndürülüyor: {first}")
    return first

# --- Düzeltilmiş: tek ve tutarlı dispatcher fonksiyonu ---

def scrape_by_university_choice(choice: str, default_year: str = "2025"):
    """
    Basit dispatcher:
    - 'istinye' -> ISTINYE_URL üzerinden scrape_generic
    - 'atlas'   -> ATLAS_URL üzerinden önce tablolar sonra extract_atlas_stream
    - 'arel'    -> AREL_PDF_URL üzerinden scrape_arel
    - URL verilmişse -> scrape_generic
    - aksi halde -> search_university_url ile bulunup scrape_generic
    """
    c = normalize_text(choice or "")

    # Direkt URL verilmişse
    if isinstance(choice, str) and (choice.startswith("http://") or choice.startswith("https://")):
        uni_name = re.sub(r"https?://(www\.)?", "", choice).split("/")[0]
        try:
            return scrape_generic(choice, uni_name, default_year=default_year)
        except Exception as e:
            print(f"[error] URL'den veri çekilemedi: {e}")
            return []

    # Bilinen anahtarlar
    if "istinye" in c:
        return scrape_generic(ISTINYE_URL, "İstinye Üniversitesi", default_year=default_year)

    if "arel" in c or "istanbul arel" in c or "arel üniversitesi" in c:
        try:
            return scrape_arel()
        except Exception as e:
            print(f"[arel] Hata: {e}")
            return []

    if "atlas" in c:
        url = ATLAS_URL
        try:
            r = requests.get(url, headers={"User-Agent": "uni-fee-bot/1.0"}, timeout=30)
            r.raise_for_status()
            time.sleep(1)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"[atlas] Sayfa indirilemedi: {e}")
            return []

        data = []
        # Tablo bazlı dene
        for table in soup.find_all("table"):
            faculty = nearest_faculty_title(table)
            rows = extract_table_rows(table, faculty, "Atlas Üniversitesi", url, default_year=default_year)
            if rows:
                data.extend(rows)
        # Akış bazlı dene
        try:
            stream_rows = extract_atlas_stream(soup)
            if stream_rows:
                data.extend(stream_rows)
        except Exception as e:
            print(f"[atlas] Akıştan çekme sırasında hata: {e}")
        return data

        found = search_university_url(source)
        if found:
            print(f"Bulunan ücret sayfası: {found}")
            uni_name = source
            try:
                return scrape_generic(found, uni_name, default_year=default_year)
            except Exception as e:
                print(f"Sayfadan veri çekme hatası: {e}")
                return []
        else:
            print("Uygun ücret sayfası bulunamadı.")
            return []

    # fallback boş liste
    return []


if __name__ == "__main__":
    all_rows = []
    # Komut satırı: ilk arg üniversite/URL, geri kalan bölüm araması
    if len(sys.argv) > 1:
        uni_arg = sys.argv[1]
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        print(f"{uni_arg} verileri çekiliyor...")
        try:
            all_rows.extend(scrape_by_university_choice(uni_arg))
        except Exception as e:
            print(f"Hata: {e}")
        print(f"Toplam kayıt: {len(all_rows)}")
        if all_rows:
            save_csv(all_rows, f"uni_fees_{normalize_text(uni_arg).replace(' ','_')}_2025.csv")
        if query:
            print(f"\nAranan bölüm: {query}")
            hits = find_fees(all_rows, query)
            print_results(hits)
        else:
            print("\nÖrnek ilk 10 kayıt:")
            print_results(all_rows[:10])
    else:
        # Etkileşimli mod: kullanıcıdan üniversite al
        uni_input = input("Hangi üniversite? (istinye/atlas veya bir URL): ").strip()
        if not uni_input:
            print("Üniversite girilmedi, çıkılıyor.")
            sys.exit(0)
        print(f"{uni_input} verileri çekiliyor...")
        try:
            all_rows.extend(scrape_by_university_choice(uni_input))
        except Exception as e:
            print(f"Hata: {e}")
        print(f"Toplam kayıt: {len(all_rows)}")
        if all_rows:
            save_csv(all_rows, f"uni_fees_{normalize_text(uni_input).replace(' ','_')}_2025.csv")
        query = input("Bölüm aramak ister misiniz? (boş bırakmak için Enter): ").strip()
        if query:
            hits = find_fees(all_rows, query)
            print_results(hits)
        else:
            print("\nÖrnek ilk 10 kayıt:")
            print_results(all_rows[:10])
