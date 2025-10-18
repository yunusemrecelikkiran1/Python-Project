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
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse, unquote, parse_qs

ISTINYE_URL = "https://aday.istinye.edu.tr/taban-puan-ve-ucretler"
ATLAS_URL   = "https://tercih.atlas.edu.tr/ogrenim-ucretleri"

# '₺485.925' / '450.000 TL' -> 485925
TL_RE = re.compile(r"(?:₺|\bTL\b)\s*([\d\.\,]+)", re.IGNORECASE)
# Atlas sayfasında satır içindeki tüm TL'leri sırayla almak için:
MONEY_IN_LINE_RE = re.compile(r"(\d[\d\.\,]*)\s*TL", re.IGNORECASE)

def parse_money(cell_text: str):
    """
    '₺485.925' / '450.000 TL' -> 485925 (int)
    Bulamazsa None döner.
    """
    m = TL_RE.search(cell_text.replace("\xa0", " "))
    if not m:
        return None
    raw = m.group(1).replace(".", "").replace(",", "")
    try:
        return int(raw)
    except ValueError:
        return None

def nearest_faculty_title(table):
    """
    Tablo üstündeki en yakın başlığı (h2/h3/strong) ipucu olarak fakülte adı say.
    """
    node = table
    for _ in range(40):
        node = node.find_previous(["h2", "h3", "strong"])
        if not node:
            break
        txt = " ".join(node.get_text(" ", strip=True).split())
        if any(k in txt for k in ["Fakültesi", "Yüksekokulu", "Meslek Yüksekokulu"]):
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

def scrape_istinye():
    return scrape_generic(ISTINYE_URL, "İstinye Üniversitesi", default_year="2025")

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
        # Fakülte başlığı ipuçları
        if ("FAKÜLTESİ" in txt) or ("YÜKSEKOKULU" in txt):
            current_faculty = txt
            continue

        # Bölüm satırı: <a> içinde isim + aynı satırda TL'ler
        if el.name == "a" and "TL" in txt:
            program = el.get_text(" ", strip=True)

            # Satırdaki TL değerlerini sırayla yakala
            nums = [parse_money(m.group(0)) for m in MONEY_IN_LINE_RE.finditer(txt)]

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

def scrape_atlas():
    r = requests.get(ATLAS_URL, headers={"User-Agent": "uni-fee-bot/1.0"}, timeout=30)
    r.raise_for_status()
    time.sleep(1)
    soup = BeautifulSoup(r.text, "html.parser")

    # 1) İhtimalen yok ama yine de: tabloları dener
    table_rows = []
    for table in soup.find_all("table"):
        faculty = nearest_faculty_title(table)
        table_rows.extend(
            extract_table_rows(table, faculty, "Atlas Üniversitesi", ATLAS_URL, default_year="2025")
        )

    # 2) Asıl yöntem: akıştan çek
    stream_rows = extract_atlas_stream(soup)

    data = []
    if table_rows:
        data.extend(table_rows)
    if stream_rows:
        data.extend(stream_rows)
    return data

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

def search_university_url(name, max_results=5):
    """
    DuckDuckGo (HTML) ile 'name öğrenim ücretleri' araması yapar.
    Bulduğu sonuçlar içinde 'ücret'/'öğrenim' anahtar kelimesi veya
    üniversite alan adını içeren ilk uygun URL'yi döndürür. Bulamazsa None.
    Daha dayanıklı olabilmesi için /l/?uddg=... linklerini çözer ve birkaç varyasyon dener.
    """
    queries = [
        f"{name} öğrenim ücretleri",
        f"{name} ücretleri",
        f"{name} öğrenim ücretleri 2025",
        f"{name} ücretleri 2025"
    ]
    ddg = "https://html.duckduckgo.com/html/"

    headers = {"User-Agent": "uni-fee-bot/1.0"}

    norm_name = "".join(ch for ch in name.lower() if ch.isalnum())

    for query in queries:
        try:
            r = requests.get(ddg, params={"q": query}, headers=headers, timeout=15)
            r.raise_for_status()
        except Exception:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        candidates = []
        # Tipik DDG sonuç linkleri
        for a in soup.select("a.result__a"):
            href = a.get("href") or a.get("data-href")
            txt = a.get_text(" ", strip=True).lower()
            if not href:
                continue
            href = _resolve_ddg_link(href)
            candidates.append((href, txt))

        # Genel link taraması (farklı yapılar için)
        if not candidates:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                txt = a.get_text(" ", strip=True).lower()
                if not href:
                    continue
                href = _resolve_ddg_link(href)
                if href.startswith("http"):
                    candidates.append((href, txt))

        # Filtreleme
        for href, txt in candidates:
            low_href = (href or "").lower()
            if any(k in txt for k in ["ücret", "ucret", "öğrenim", "ogrenim"]) or any(k in low_href for k in ["ucret", "ücret", "ogrenim", "öğrenim"]):
                return href
            try:
                net = urlparse(href).netloc.lower()
                if norm_name and norm_name in net:
                    return href
            except Exception:
                continue
    return None

def scrape_by_university_choice(choice: str):
    c = normalize_text(choice)
    # Bilinen üniversiteler
    if "istinye" in c:
        return scrape_istinye()
    if "atlas" in c:
        return scrape_atlas()
    # Eğer tam URL verilmişse
    if choice.startswith("http://") or choice.startswith("https://"):
        # üniversite adı olarak alan adını kullan
        uni_name = re.sub(r"https?://(www\.)?", "", choice).split("/")[0]
        return scrape_generic(choice, uni_name, default_year="2025")

    # Yeni: isim girilmişse web araması yap ve uygun URL'den çek
    print(f"'{choice}' için web araması yapılıyor...")
    found = search_university_url(choice)
    if found:
        print(f"Bulunan sayfa: {found}")
        # isim olarak kullanıcı girdiğini dosya adı için kullan
        uni_name = choice
        try:
            return scrape_generic(found, uni_name, default_year="2025")
        except Exception as e:
            print(f"Sayfadan veri çekme hatası: {e}")
            return []
    else:
        # İkinci deneme: 'site:.edu.tr' ile daha spesifik arama (DuckDuckGo parametre sınırlamaları nedeniyle aynı uç nokta)
        print("İlk aramada uygun sayfa bulunamadı, alternatif arama yapılıyor...")
        found2 = search_university_url(choice + " site:.edu.tr")
        if found2:
            print(f"Bulunan sayfa: {found2}")
            try:
                return scrape_generic(found2, choice, default_year="2025")
            except Exception as e:
                print(f"Sayfadan veri çekme hatası: {e}")
                return []
    # Bulunamadıysa kullanıcıya bildir
    print("Bilinmeyen üniversite veya uygun sayfa bulunamadı. Geçerli örnekler: istinye, atlas veya tam URL.")
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
