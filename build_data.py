"""從內政部實價登錄公開資料產生遊戲題庫 data.js

用法: python build_data.py <zip> [<zip> ...]
可以同時給本期、前期、季資料的 zip，重複的交易會用編號去重，
只保留最新成交日往前 MONTHS 個月內的交易。
平常不用手動跑，update_data.py 會自動下載並呼叫這裡。
"""
import csv
import datetime as dt
import io
import json
import re
import statistics
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

CITY = {
    "a": "臺北市", "b": "臺中市", "c": "基隆市", "d": "臺南市", "e": "高雄市",
    "f": "新北市", "g": "宜蘭縣", "h": "桃園市", "i": "嘉義市", "j": "新竹縣",
    "k": "苗栗縣", "m": "南投縣", "n": "彰化縣", "o": "新竹市", "p": "雲林縣",
    "q": "嘉義縣", "s": "高雄市", "t": "屏東縣", "u": "花蓮縣", "v": "臺東縣",
    "w": "金門縣", "x": "澎湖縣", "z": "連江縣",
}

TYPES = {
    "公寓": "公寓", "透天厝": "透天厝", "華廈": "華廈", "住宅大樓": "住宅大樓", "套房": "套房",
}

# 備註出現這些字的交易價格不具參考性，排除
BAD_NOTE = re.compile(
    "親|特殊|關係|員工|瑕疵|凶|債|拍賣|急|持分|共有|政府|法人|頂讓|地上權|分次|預售|調整|"
    "贈與|夾層|違建|增建|未登記|加蓋|裝潢|傢俱|家具|含其他|租約|讓渡|身故|毗鄰|合建|合併|"
    "畸零|袋地|毀損|海砂|輻射|漏水|佔用|占用|其他")

NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

SQM_TO_PING = 0.3025
MONTHS = 2   # 題庫保留近幾個月的成交


def zh_num(s):
    """'二十三' -> 23；失敗回 None"""
    if not s:
        return None
    if s.startswith("十"):
        s = "一" + s
    total, cur = 0, 0
    for ch in s:
        if ch in NUM:
            cur = NUM[ch]
        elif ch == "十":
            total += cur * 10
            cur = 0
        elif ch == "百":
            total += cur * 100
            cur = 0
        else:
            return None
    return total + cur


def floor_of(s):
    m = re.fullmatch(r"(.+?)層", s or "")
    return zh_num(m.group(1)) if m else None


def halfwidth(s):
    return "".join(chr(ord(c) - 0xFEE0) if "！" <= c <= "～" else c for c in s).replace("　", " ")


def roc_date(s):
    """'0830624' -> (83, 6, 24)"""
    s = (s or "").strip()
    if not re.fullmatch(r"\d{6,7}", s):
        return None
    s = s.zfill(7)
    y, m, d = int(s[:3]), int(s[3:5]), int(s[5:7])
    if not (1 <= m <= 12):
        return None
    return y, m, max(d, 1)


def to_float(s):
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_row(code, r):
    dist, target, addr = r[0], r[1], halfwidth(r[2]).strip()
    if not target.startswith("房地"):
        return None
    btype = next((v for k, v in TYPES.items() if r[11].startswith(k)), None)
    if not btype or "住" not in r[12]:
        return None
    if not re.search(r"建物1", r[8]) or "號" not in addr:
        return None
    note = r[26].strip()
    if note and BAD_NOTE.search(note):
        return None

    total = int(to_float(r[21]))
    park_price = int(to_float(r[25]))
    park_area = to_float(r[24])
    has_park = bool(r[23].strip())
    price = total - park_price if park_price > 0 else total
    area = to_float(r[15]) - (park_area if park_price > 0 else 0)
    if area < 10 or not (3_000_000 <= price <= 200_000_000):
        return None

    trade = roc_date(r[7])
    built = roc_date(r[14])
    if not trade:
        return None
    age = None
    if built:
        age = trade[0] - built[0] - (1 if (trade[1], trade[2]) < (built[1], built[2]) else 0)
        age = max(age, 0)

    city = CITY[code]
    if not addr.startswith(city):
        addr = city + (dist if dist != city and not addr.startswith(dist) else "") + addr

    addr = addr.replace(city + city, city)

    total_floor = floor_of(r[10])
    if r[9] == "全":
        floor_txt = f"整棟，共{total_floor}樓" if total_floor else "整棟"
    else:
        floors = [floor_of(x) for x in r[9].split("，")]
        floors = [f for f in floors if f]
        if not floors:
            return None
        floor_txt = "、".join(f"{f}樓" for f in floors) + (f"／共{total_floor}樓" if total_floor else "")

    main, aux, balcony = to_float(r[28]), to_float(r[29]), to_float(r[30])
    public_ratio = None
    if btype != "透天厝" and main > 0 and area > 0:
        public_ratio = round((area - main - aux - balcony) / area * 100)
        if not (0 <= public_ratio <= 70):
            public_ratio = None

    ping = area * SQM_TO_PING
    h = {
        "city": city,
        "dist": dist,
        "addr": addr,
        "type": btype,
        "floor": floor_txt,
        "ping": round(ping, 1),
        "age": age,
        "built": f"民國{built[0]}年{built[1]:02d}月{built[2]:02d}日" if built else None,
        "elevator": r[31].strip() or None,
        "mgmt": r[20].strip() or None,
        "layout": f"{r[16]}房{r[17]}廳{r[18]}衛" if r[16] != "0" or r[17] != "0" else None,
        "material": r[13].strip() or None,
        "trade": f"民國{trade[0]}年{trade[1]:02d}月",
        "_id": r[27].strip(),
        "_date": dt.date(trade[0] + 1911, trade[1], trade[2]),
        "price": round(price / 10000),  # 萬
        "unit": round(price / 10000 / ping, 1),  # 萬/坪
    }
    if public_ratio is not None:
        h["publicRatio"] = public_ratio
    if btype == "透天厝":
        land = to_float(r[3]) * SQM_TO_PING
        if land > 0:
            h["land"] = round(land, 1)
    if has_park:
        h["park"] = r[23].strip() + ("，總價不含車位" if park_price > 0 else "，總價含車位")
    return h


def read_zip(zip_path):
    z = zipfile.ZipFile(zip_path)
    for name in sorted(z.namelist()):
        m = re.fullmatch(r"([a-z])_lvr_land_a\.csv", name)
        if not m or m.group(1) not in CITY:
            continue
        rows = list(csv.reader(io.StringIO(z.read(name).decode("utf-8-sig"))))[2:]
        for r in rows:
            if len(r) < 32:
                continue
            try:
                h = parse_row(m.group(1), r)
            except ValueError:   # 日期等欄位格式異常
                continue
            if h:
                yield h


def build(zip_paths, out=None):
    seen, houses = set(), []
    for zp in zip_paths:
        for h in read_zip(zp):
            key = h["_id"] or (h["addr"], h["trade"], h["price"])
            if key in seen:
                continue
            seen.add(key)
            houses.append(h)
    if not houses:
        raise SystemExit("沒有可用的交易資料")

    newest = max(h["_date"] for h in houses)
    start = newest - dt.timedelta(days=round(MONTHS * 30.5))
    houses = [h for h in houses if start <= h["_date"] <= newest]
    houses.sort(key=lambda h: h["_date"])

    # 中位數：全國同型態、同縣市同型態
    by_type, by_city_type = defaultdict(list), defaultdict(list)
    for h in houses:
        by_type[h["type"]].append(h["price"])
        by_city_type[(h["city"], h["type"])].append(h["price"])
    nat = {t: round(statistics.median(v)) for t, v in by_type.items()}
    for h in houses:
        h["median"] = round(statistics.median(by_city_type[(h["city"], h["type"])]))
        del h["_id"], h["_date"]

    roc = lambda d: f"民國{d.year - 1911}年{d.month}月{d.day}日"
    out = Path(out) if out else Path(__file__).with_name("data.js")
    payload = {
        "range": f"{roc(start)}～{roc(newest)}",
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "nationalMedian": nat,
        "houses": houses,
    }
    tmp = out.with_suffix(".tmp")
    tmp.write_text("window.HOUSE_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    tmp.replace(out)
    print(f"{len(houses)} 筆（成交日 {payload['range']}）→ {out}")
    for t, v in sorted(nat.items()):
        print(f"  {t}: 中位數 {v} 萬（{len(by_type[t])} 筆）")
    return len(houses)


if __name__ == "__main__":
    build(sys.argv[1:])
