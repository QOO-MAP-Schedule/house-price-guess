"""每天自動更新題庫：從內政部實價登錄下載最新資料，重建 data.js

下載三種資料（已下載過的不會重抓）：
  - 本期（最近一期，每月 1、11、21 日發布）
  - 前期（網站上列出的前幾期）
  - 最新一季
合併後交給 build_data.build()，只保留近 MONTHS 個月的成交。

用法: python update_data.py        （Windows 工作排程器每天執行）
紀錄寫在 update.log
"""
import datetime as dt
import logging
import re
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_data  # noqa: E402

BASE = "https://plvr.land.moi.gov.tw/"
RAW = HERE / "raw"
KEEP_SEASONS = 2
KEEP_PERIOD_DAYS = 150

logging.basicConfig(
    filename=HERE / "update.log", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger()


# 內政部網站憑證缺 Subject Key Identifier，Python 3.13+ 的嚴格模式會拒絕；
# 仍然驗證憑證鏈與主機名稱，只關掉 X509 嚴格檢查
SSL_CTX = ssl.create_default_context()
SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT


def fetch(url, timeout=300):
    req = urllib.request.Request(BASE + url, headers={"User-Agent": "Mozilla/5.0 house-price-guess"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def save_zip(url, dest):
    data = fetch(url)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    try:
        with zipfile.ZipFile(tmp) as z:
            if not any(n.endswith("_lvr_land_a.csv") for n in z.namelist()):
                raise zipfile.BadZipFile("缺少 lvr_land_a.csv")
    except zipfile.BadZipFile as e:
        tmp.unlink()
        raise RuntimeError(f"{url} 不是有效的資料檔：{e}")
    tmp.replace(dest)
    log.info("下載 %s（%.1f MB）", dest.name, len(data) / 1e6)


def main():
    RAW.mkdir(exist_ok=True)
    errors = []

    # 前期：DownloadHistory_ajax_list 頁面上的 downloadLast('YYYYMMDD')
    try:
        page = fetch("DownloadHistory_ajax_list", 60).decode("utf-8", "replace")
        for d in sorted(set(re.findall(r"downloadLast\('(\d{8})'\)", page))):
            dest = RAW / f"period_{d}.zip"
            if not dest.exists():
                save_zip(f"DownloadHistory?type=history&fileName={d}", dest)
    except Exception as e:
        errors.append(f"前期：{e}")

    # 最新一季：DownloadSeason_ajax_list 的第一個選項，例如 115S3
    try:
        page = fetch("DownloadSeason_ajax_list", 60).decode("utf-8", "replace")
        seasons = re.findall(r'<option value="(\d{3}S[1-4])"', page)
        if seasons:
            dest = RAW / f"season_{seasons[0]}.zip"
            if not dest.exists():
                save_zip(f"DownloadSeason?season={seasons[0]}&type=zip&fileName=lvr_landcsv.zip", dest)
    except Exception as e:
        errors.append(f"季資料：{e}")

    # 本期：檔案小，每次都重抓
    try:
        save_zip("Download?type=zip&fileName=lvr_landcsv.zip", RAW / "current.zip")
    except Exception as e:
        errors.append(f"本期：{e}")

    # 清掉用不到的舊檔
    for old in sorted(RAW.glob("season_*.zip"))[:-KEEP_SEASONS]:
        old.unlink()
        log.info("刪除舊季資料 %s", old.name)
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_PERIOD_DAYS)).strftime("%Y%m%d")
    for old in RAW.glob("period_*.zip"):
        if old.stem.split("_")[1] < cutoff:
            old.unlink()
            log.info("刪除舊期資料 %s", old.name)

    for e in errors:
        log.warning(e)

    # 新的檔案排前面，同一筆交易以較新的資料為準
    zips = [RAW / "current.zip"] if (RAW / "current.zip").exists() else []
    zips += sorted(RAW.glob("period_*.zip"), reverse=True)
    zips += sorted(RAW.glob("season_*.zip"), reverse=True)
    if not zips:
        log.error("沒有任何資料檔，題庫未更新")
        return 1
    n = build_data.build(zips)
    log.info("題庫更新完成：%d 筆，來源 %s", n, ", ".join(z.name for z in zips))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("更新失敗")
        sys.exit(1)
