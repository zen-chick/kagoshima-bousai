#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
鹿児島 防災ナビ  データ取得スクリプト

GitHub Actions から定期実行し、data/*.json を更新する。
ブラウザから気象庁を直接叩かないので CORS の影響を受けず、
生成された JSON は Service Worker でそのままオフラインキャッシュできる。

出力:
  data/now.json      現在の天気・風・雨・警報・噴火警戒レベル・雨雲タイルの時刻
  data/rain.json     日別降水量（過去約100日）と 今日/1ヶ月/3ヶ月 の累計
  data/transit.json  公共交通機関の運行情報リンク（状態は best-effort）

使い方:
  python3 scripts/fetch_data.py            # 通常（now + transit）
  python3 scripts/fetch_data.py --daily    # 日次（rain も更新）
"""

import json
import os
import re
import sys
import time
import datetime as dt
import urllib.request
import urllib.error
from html.parser import HTMLParser

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    JST = dt.timezone(dt.timedelta(hours=9))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# ---------------------------------------------------------------- 設定
OFFICE_CODE = "460100"   # 鹿児島県（薩摩地方）予報区
PREF_CODE = "460000"     # 鹿児島県 警報・注意報
AMEDAS_ID = "88317"      # アメダス 鹿児島
ETRN_PREC = "88"         # 過去データ 鹿児島県
ETRN_BLOCK = "47827"     # 過去データ 鹿児島（地方気象台）
AREA_NAME = "鹿児島市"    # 警報を絞り込む市町村名（部分一致）

UA = "kagoshima-bousai/3.0 (+https://zen-chick.github.io/kagoshima-bousai/)"

# 平年値 1991-2020 鹿児島（mm）※公式値に置き換えたい場合はここだけ直す
NORMAL_MONTHLY = [78.3, 112.7, 161.0, 194.9, 205.2, 570.0,
                  365.1, 224.3, 222.9, 104.6, 102.5, 93.2]

WARNING_NAMES = {
    "00": "解除", "02": "暴風雪警報", "03": "大雨警報", "04": "洪水警報",
    "05": "暴風警報", "06": "大雪警報", "07": "波浪警報", "08": "高潮警報",
    "10": "大雨注意報", "12": "大雪注意報", "13": "風雪注意報", "14": "雷注意報",
    "15": "強風注意報", "16": "波浪注意報", "17": "融雪注意報", "18": "洪水注意報",
    "19": "高潮注意報", "20": "濃霧注意報", "21": "乾燥注意報", "22": "なだれ注意報",
    "23": "低温注意報", "24": "霜注意報", "25": "着氷注意報", "26": "着雪注意報",
    "27": "その他の注意報", "32": "暴風雪特別警報", "33": "大雨特別警報",
    "35": "暴風特別警報", "36": "大雪特別警報", "37": "波浪特別警報",
    "38": "高潮特別警報",
}

WIND_16 = ["静穏", "北北東", "北東", "東北東", "東", "東南東", "南東", "南南東",
           "南", "南南西", "南西", "西南西", "西", "西北西", "北西", "北北西", "北"]

TRANSIT_LINKS = [
    {"name": "JR九州 鹿児島エリア", "kind": "鉄道",
     "url": "https://www.jrkyushu.co.jp/trains/info/kagoshima.html"},
    {"name": "九州新幹線", "kind": "新幹線",
     "url": "https://www.jrkyushu.co.jp/trains/info/shin.html"},
    {"name": "鹿児島市電・市バス（交通局）", "kind": "市内",
     "url": "https://www.kotsu-city-kagoshima.jp/"},
    {"name": "南国交通", "kind": "バス",
     "url": "https://nangoku-kotsu.com/"},
    {"name": "いわさきバスネットワーク", "kind": "バス",
     "url": "https://iwasaki-corp.com/bus/"},
    {"name": "桜島フェリー", "kind": "航路",
     "url": "https://www.city.kagoshima.lg.jp/sakurajima-ferry/"},
]


# ---------------------------------------------------------------- 共通
def log(*a):
    print("[fetch]", *a, file=sys.stderr, flush=True)


def get(url, binary=False, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    log("GET failed:", url, last)
    return None


def get_json(url):
    t = get(url)
    if t is None:
        return None
    try:
        return json.loads(t)
    except Exception as e:  # noqa: BLE001
        log("JSON parse failed:", url, e)
        return None


def read_existing(name):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return {}


def write_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    log("wrote", name)


def now_jst():
    return dt.datetime.now(JST)


# ---------------------------------------------------------------- 天気予報
def weather_icon(code):
    """気象庁 weatherCode → 絵文字。100番台=晴, 200=曇, 300=雨, 400=雪。"""
    try:
        n = int(str(code))
    except Exception:  # noqa: BLE001
        return "❓"
    if 100 <= n < 200:
        if n in (100, 123, 124, 130, 131):
            return "☀️"
        if n in (101, 132):
            return "🌤"
        if n in (102, 103, 106, 107, 108, 120, 121, 140, 160, 170, 181):
            return "🌦"
        if n in (104, 105, 160, 170):
            return "🌨"
        return "🌤"
    if 200 <= n < 300:
        if n in (200, 209, 231):
            return "☁️"
        if n in (201, 223):
            return "⛅"
        if n in (202, 203, 206, 207, 208, 220, 221, 240, 250, 260, 270, 281):
            return "🌧"
        return "☁️"
    if 300 <= n < 400:
        if n in (306, 328, 329, 350):
            return "🌧"
        if n in (302, 303, 308, 309, 322, 323, 324, 325):
            return "🌦"
        if n in (340, 361, 371):
            return "🌨"
        return "🌧"
    if 400 <= n < 500:
        return "❄️"
    return "❓"


def fetch_forecast():
    js = get_json(f"https://www.jma.go.jp/bosai/forecast/data/forecast/{OFFICE_CODE}.json")
    out = {"text": "—", "icon": "❓", "code": None,
           "tempMax": None, "tempMin": None, "pop": None, "headline": ""}
    if not js:
        return out
    try:
        ts = js[0]["timeSeries"]
        area = ts[0]["areas"][0]
        out["code"] = area["weatherCodes"][0]
        out["text"] = re.sub(r"\u3000+", " ", area["weathers"][0]).strip()
        out["icon"] = weather_icon(out["code"])
        out["wind"] = re.sub(r"\u3000+", " ", area.get("winds", [""])[0]).strip()
        # 降水確率
        for s in ts:
            a0 = s["areas"][0]
            if "pops" in a0:
                pops = [p for p in a0["pops"] if p not in ("", None)]
                if pops:
                    out["pop"] = int(pops[0])
                break
        # 気温
        for s in ts:
            a0 = s["areas"][0]
            if "temps" in a0:
                temps = [t for t in a0["temps"] if t not in ("", None)]
                if temps:
                    out["tempMin"] = float(temps[0])
                    out["tempMax"] = float(temps[-1])
                break
    except Exception as e:  # noqa: BLE001
        log("forecast parse:", e)

    ov = get_json(f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{OFFICE_CODE}.json")
    if ov:
        out["headline"] = (ov.get("headlineText") or "").strip()
    return out


# ---------------------------------------------------------------- 警報・注意報
def fetch_warnings():
    js = get_json(f"https://www.jma.go.jp/bosai/warning/data/warning/{PREF_CODE}.json")
    items, level = [], "none"
    if not js:
        return items, level, None
    try:
        for at in js.get("areaTypes", []):
            for area in at.get("areas", []):
                nm = area.get("name") or ""
                if AREA_NAME not in nm:
                    continue
                for w in area.get("warnings", []):
                    st = w.get("status", "")
                    if st in ("解除", "なし", ""):
                        continue
                    code = str(w.get("code", ""))
                    name = WARNING_NAMES.get(code, f"警報等({code})")
                    cond = (w.get("condition") or "").strip()
                    if "特別警報" in name:
                        lv = "emergency"
                    elif "警報" in name:
                        lv = "warning"
                    else:
                        lv = "advisory"
                    items.append({"code": code, "name": name, "condition": cond,
                                  "level": lv, "area": nm, "status": st})
    except Exception as e:  # noqa: BLE001
        log("warning parse:", e)

    # 重複除去
    seen, uniq = set(), []
    for it in items:
        k = (it["code"], it["condition"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    order = {"emergency": 3, "warning": 2, "advisory": 1, "none": 0}
    for it in uniq:
        if order[it["level"]] > order[level]:
            level = it["level"]
    uniq.sort(key=lambda x: -order[x["level"]])
    return uniq, level, (js.get("reportDatetime") if js else None)


# ---------------------------------------------------------------- アメダス
def fetch_amedas():
    out = {"station": "鹿児島", "time": None, "temp": None,
           "windDir": None, "windDeg": None, "wind": None, "gust": None,
           "rain10m": None, "rain1h": None, "rain3h": None, "rain24h": None}
    latest = get("https://www.jma.go.jp/bosai/amedas/data/latest_time.txt")
    if not latest:
        return out
    latest = latest.strip()
    try:
        t = dt.datetime.fromisoformat(latest)
    except Exception:  # noqa: BLE001
        return out

    block = (t.hour // 3) * 3
    candidates = [t, t - dt.timedelta(hours=3)]
    for base in candidates:
        blk = (base.hour // 3) * 3
        url = ("https://www.jma.go.jp/bosai/amedas/data/point/"
               f"{AMEDAS_ID}/{base.strftime('%Y%m%d')}_{blk:02d}.json")
        js = get_json(url)
        if not js:
            continue
        keys = sorted(js.keys())
        if not keys:
            continue
        k = keys[-1]
        v = js[k]
        try:
            out["time"] = dt.datetime.strptime(k, "%Y%m%d%H%M%S").replace(
                tzinfo=JST).isoformat()
        except Exception:  # noqa: BLE001
            out["time"] = latest

        def num(key):
            x = v.get(key)
            if isinstance(x, list) and x:
                try:
                    return float(x[0])
                except Exception:  # noqa: BLE001
                    return None
            return None

        out["temp"] = num("temp")
        out["wind"] = num("wind")
        out["gust"] = num("gustWind") if "gustWind" in v else num("gust")
        d = v.get("windDirection")
        if isinstance(d, list) and d:
            try:
                idx = int(d[0])
                out["windDeg"] = 0 if idx == 0 else (idx % 16) * 22.5
                out["windDir"] = WIND_16[idx] if 0 <= idx < 17 else None
            except Exception:  # noqa: BLE001
                pass
        out["rain10m"] = num("precipitation10m")
        out["rain1h"] = num("precipitation1h")
        out["rain3h"] = num("precipitation3h")
        out["rain24h"] = num("precipitation24h")
        break
    _ = block
    return out


# ---------------------------------------------------------------- 雨雲タイル時刻
def fetch_nowcast_times():
    out = {"basetime": None, "validtime": None, "forecast": []}
    js = get_json("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json")
    if isinstance(js, list) and js:
        last = js[-1]
        out["basetime"] = last.get("basetime")
        out["validtime"] = last.get("validtime")
    js2 = get_json("https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json")
    if isinstance(js2, list):
        out["forecast"] = [{"basetime": x.get("basetime"),
                            "validtime": x.get("validtime")} for x in js2[:6]]
    return out


# ---------------------------------------------------------------- 記録的短時間大雨情報
def fetch_heavy_rain_flash():
    """防災情報XML（extra.xml）から記録的短時間大雨情報を拾う。"""
    xml = get("https://www.data.jma.go.jp/developer/xml/feed/extra.xml")
    if not xml:
        return None
    try:
        entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
        for e in entries:
            title = re.search(r"<title>(.*?)</title>", e, re.S)
            if not title or "記録的短時間大雨情報" not in title.group(1):
                continue
            content = re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
            body = content.group(1) if content else ""
            if "鹿児島" not in body:
                continue
            updated = re.search(r"<updated>(.*?)</updated>", e, re.S)
            when = updated.group(1) if updated else None
            if when:
                try:
                    tt = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if (dt.datetime.now(dt.timezone.utc) - tt).total_seconds() > 6 * 3600:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            return {"text": re.sub(r"\s+", " ", body).strip()[:300], "time": when}
    except Exception as ex:  # noqa: BLE001
        log("extra.xml parse:", ex)
    return None


# ---------------------------------------------------------------- 噴火警戒レベル
def fetch_volcano():
    """桜島の噴火警戒レベル。取得できなければ前回値を維持する。"""
    prev = read_existing("now.json").get("volcano") or {
        "name": "桜島", "level": 3, "text": "入山規制", "updated": None}
    js = get_json("https://www.jma.go.jp/bosai/volcano/data/forecast/volcano.json")
    if isinstance(js, list):
        for v in js:
            nm = str(v.get("volcanoName") or v.get("name") or "")
            if "桜島" in nm:
                lv = v.get("level") or v.get("warningLevel")
                try:
                    prev["level"] = int(str(lv))
                except Exception:  # noqa: BLE001
                    pass
                prev["updated"] = v.get("reportDatetime") or prev.get("updated")
                break
    labels = {1: "活火山であることに留意", 2: "火口周辺規制", 3: "入山規制",
              4: "高齢者等避難", 5: "避難"}
    prev["text"] = labels.get(prev.get("level"), prev.get("text", ""))
    return prev


# ---------------------------------------------------------------- 過去の降水量
class DailyTableParser(HTMLParser):
    """気象庁 過去の気象データ（日ごとの値）の表を抜き出す。"""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag == "td":
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr":
            if len(self.row) >= 6:
                self.rows.append(self.row)
            self.row = []


def parse_mm(s):
    s = (s or "").strip()
    if s in ("", "--", "///", "×", "#"):
        return 0.0
    s = s.replace(")", "").replace("]", "").replace("(", "").replace("[", "")
    try:
        return float(s)
    except Exception:  # noqa: BLE001
        return 0.0


def fetch_month_rain(year, month):
    url = ("https://www.data.jma.go.jp/obd/stats/etrn/view/daily_s1.php"
           f"?prec_no={ETRN_PREC}&block_no={ETRN_BLOCK}"
           f"&year={year}&month={month}&day=&view=")
    html = get(url)
    if not html:
        return {}
    p = DailyTableParser()
    try:
        p.feed(html)
    except Exception as e:  # noqa: BLE001
        log("daily table:", e)
        return {}
    out = {}
    for row in p.rows:
        day = row[0].strip()
        if not day.isdigit():
            continue
        # 列: 日 / 気圧(現地) / 気圧(海面) / 降水量合計 / 最大1時間 / 最大10分
        mm = parse_mm(row[3]) if len(row) > 3 else 0.0
        try:
            d = dt.date(year, month, int(day)).isoformat()
        except ValueError:
            continue
        out[d] = mm
    return out


def build_rain():
    today = now_jst().date()
    days = {}
    prev = read_existing("rain.json")
    for d in (prev.get("days") or []):
        days[d["d"]] = d["mm"]

    # 直近4ヶ月分を取り直す（月替わりの取りこぼし防止）
    y, m = today.year, today.month
    for _ in range(4):
        got = fetch_month_rain(y, m)
        if got:
            days.update(got)
        m -= 1
        if m == 0:
            y, m = y - 1, 12

    series = sorted(days.items())
    series = series[-140:]
    days_list = [{"d": d, "mm": round(v, 1)} for d, v in series if d <= today.isoformat()]

    def total(n):
        start = (today - dt.timedelta(days=n - 1)).isoformat()
        return round(sum(x["mm"] for x in days_list if x["d"] >= start), 1)

    def normal_window(n):
        # 平年月値を日割りして期間に按分する簡易推定
        s = 0.0
        for i in range(n):
            d = today - dt.timedelta(days=i)
            dim = (dt.date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
                   - dt.date(d.year, d.month, 1)).days
            s += NORMAL_MONTHLY[d.month - 1] / dim
        return round(s, 1)

    today_mm = next((x["mm"] for x in days_list if x["d"] == today.isoformat()), 0.0)

    return {
        "updated": now_jst().isoformat(timespec="seconds"),
        "station": "鹿児島",
        "days": days_list,
        "sum": {"today": today_mm, "d30": total(30), "d90": total(90), "d7": total(7)},
        "normal": {"monthly": NORMAL_MONTHLY,
                   "d30": normal_window(30), "d90": normal_window(90)},
    }


# ---------------------------------------------------------------- 交通
def build_transit():
    """公共交通の遅延には公開APIがないため、リンク＋best-effort判定にとどめる。
    自動判定に自信が持てない場合は status を unknown のままにする。"""
    ops = []
    for t in TRANSIT_LINKS:
        item = dict(t)
        item["status"] = "unknown"
        item["note"] = ""
        if os.environ.get("BOUSAI_PROBE_TRANSIT") == "1" and t["kind"] in ("鉄道", "新幹線"):
            html = get(t["url"])
            if html:
                txt = re.sub(r"<[^>]+>", " ", html)
                if re.search(r"平常(どおり|通り)", txt):
                    item["status"] = "normal"
                elif re.search(r"遅[れn延]|運転見合わせ|運休|見合わせ", txt):
                    item["status"] = "alert"
                    mm = re.search(r"[^。]{0,60}(遅れ|運転見合わせ|運休)[^。]{0,60}", txt)
                    if mm:
                        item["note"] = re.sub(r"\s+", " ", mm.group(0)).strip()[:120]
        ops.append(item)
    return {"updated": now_jst().isoformat(timespec="seconds"), "operators": ops}


# ---------------------------------------------------------------- main
def main():
    daily = "--daily" in sys.argv
    os.makedirs(DATA, exist_ok=True)

    warnings, level, report = fetch_warnings()
    now = {
        "updated": now_jst().isoformat(timespec="seconds"),
        "reportDatetime": report,
        "weather": fetch_forecast(),
        "amedas": fetch_amedas(),
        "warnings": warnings,
        "warningLevel": level,
        "heavyRainFlash": fetch_heavy_rain_flash(),
        "volcano": fetch_volcano(),
        "nowcast": fetch_nowcast_times(),
    }
    write_json("now.json", now)
    write_json("transit.json", build_transit())

    if daily or not os.path.exists(os.path.join(DATA, "rain.json")):
        write_json("rain.json", build_rain())


if __name__ == "__main__":
    main()
