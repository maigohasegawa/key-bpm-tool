#!/usr/bin/env python3
"""
キー/BPM 照合ツール（マッシュアップ用）

同じフォルダの起動ファイル（Mac: .command / Windows: .bat）をダブルクリックすると、
ブラウザに画面が開きます。Spotify のプレイリストURLを貼り付けて「解析」を押すだけです。

- 曲情報（曲名・アーティスト）から、楽曲データベースの BPM/キーを引きます。音源は使いません。
- 複数のデータベース（ReccoBeats / GetSongBPM / SoundStat）の値を照合し、ずれている曲に印を付けます。
- 全曲の総当たりで、BPM とキーが近いマッシュアップ候補を探します。

標準ライブラリだけで動きます。終了するときは画面右上の「終了」を押すか、黒いウィンドウを閉じてください。
"""

import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "キー/BPM 照合ツール"
HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "保存データ"
CONFIG = DATA_DIR / "settings.json"
CACHE_FILE = DATA_DIR / "cache.json"
HISTORY_DIR = DATA_DIR / "history"
PORT = 8765

RECCO = "https://api.reccobeats.com/v1"
GSBPM = "https://api.getsong.co"
SOUNDSTAT = "https://soundstat.info/api/v1"
BATCH = 40
CACHE_DAYS = 30
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TRACK_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?track/|spotify:track:)([A-Za-z0-9]{22})")
LIST_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?|spotify:)(playlist|album)[/:]([A-Za-z0-9]{22})")
BARE_RE = re.compile(r"^[A-Za-z0-9]{22}$")
NOTE_INDEX = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


class AuthError(Exception):
    pass


class UserError(Exception):
    pass


# ================================================================ network

def _mac_system_roots():
    """python.org 版 Python は証明書を持たないため、macOS 本体の証明書を書き出して使う"""
    import subprocess
    pem = DATA_DIR / "mac_root_certs.pem"
    try:
        if pem.exists() and pem.stat().st_size > 10000 and time.time() - pem.stat().st_mtime < 30 * 86400:
            return pem
        out = b""
        for kc in ("/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain"):
            r = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", kc],
                               capture_output=True, timeout=20)
            if r.returncode == 0:
                out += r.stdout
        if b"BEGIN CERTIFICATE" in out:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            pem.write_bytes(out)
            return pem
    except Exception:
        pass
    return None


def _ctx():
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and ctx.cert_store_stats().get("x509_ca", 0) == 0:
        pem = _mac_system_roots()
        if pem:
            try:
                ctx.load_verify_locations(cafile=str(pem))
            except Exception:
                pass
    return ctx


CTX = _ctx()


def http_get(url, headers=None, retries=3, accept="application/json"):
    h = {"Accept": accept, "User-Agent": "key-bpm-app/3.0"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=25) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = e.headers.get("Retry-After") or "5"
                time.sleep(min(int(wait) if wait.isdigit() else 5, 60))
                continue
            if e.code in (401, 403):
                raise AuthError(str(e.code))
            if e.code == 404:
                return 404, ""
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(2)
                continue
            raise
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                raise UserError("SSL証明書の確認に失敗しました。Finder で「アプリケーション」→「Python 3.x」フォルダを開き、"
                                "「Install Certificates.command」をダブルクリックしてから、ツールを起動し直してください。")
            if attempt < retries - 1:
                time.sleep(2)
                continue
            raise UserError(f"インターネットに接続できませんでした（{e.reason}）")
    return 0, ""


def get_json(url, headers=None, retries=3):
    status, body = http_get(url, headers, retries)
    if status == 202:
        return {"__pending__": True}
    if status == 404 or not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# ================================================================ storage

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_settings():
    s = load_json(CONFIG, {})
    # 旧版の設定ファイルも読む
    old = load_json(HERE / "key_bpm_config.json", {})
    for k in ("getsongbpm_api_key", "soundstat_api_key"):
        if not s.get(k) and old.get(k):
            s[k] = old[k]
    return s


class Cache:
    def __init__(self):
        self.data = load_json(CACHE_FILE, {})
        self.lock = threading.Lock()

    def get(self, source, sid):
        v = self.data.get(source, {}).get(sid)
        if v and time.time() - v.get("_t", 0) < CACHE_DAYS * 86400:
            return v
        return None

    def put(self, source, sid, value):
        value = dict(value)
        value["_t"] = int(time.time())
        with self.lock:
            self.data.setdefault(source, {})[sid] = value

    def drop(self, sids):
        with self.lock:
            for src in self.data.values():
                for sid in sids:
                    src.pop(sid, None)

    def save(self):
        with self.lock:
            save_json(CACHE_FILE, self.data)


CACHE = Cache()


def clean(v):
    return {k: val for k, val in v.items() if not k.startswith("_")} if v else v


# ================================================================ helpers

def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"\s+-\s+.*$", "", s)
    s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)
    s = re.sub(r"\b(feat|ft)\.?\s.*$", "", s)
    return re.sub(r"[^\w]", "", s)


def parse_key_name(text):
    if not text:
        return None, None
    m = re.match(r"^\s*([A-Ga-g])\s*([#♯b♭]?)\s*(m(?:in(?:or)?)?|maj(?:or)?)?\s*$", str(text))
    if not m:
        return None, None
    k = NOTE_INDEX[m.group(1).upper()]
    if m.group(2) in ("#", "♯"):
        k += 1
    elif m.group(2) in ("b", "♭"):
        k -= 1
    q = m.group(3) or ""
    return k % 12, (0 if q.startswith("m") and not q.startswith("maj") else 1)


def num(v):
    try:
        f = float(v)
        return round(f, 2) if f > 0 else None
    except (TypeError, ValueError):
        return None


def spotify_id_of(item):
    m = TRACK_RE.search(item.get("href") or "")
    return m.group(1) if m else None


# ================================================================ input → tracks

def find_track_list(obj):
    """埋め込みページの JSON から曲リストを探す"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and \
                sum(1 for x in obj if str(x.get("uri", "")).startswith("spotify:track:")) >= max(1, len(obj) // 2):
            return obj
        for x in obj:
            r = find_track_list(x)
            if r:
                return r
    elif isinstance(obj, dict):
        if isinstance(obj.get("trackList"), list):
            return obj["trackList"]
        for v in obj.values():
            r = find_track_list(v)
            if r:
                return r
    return None


def find_entity_name(obj):
    try:
        e = obj["props"]["pageProps"]["state"]["data"]["entity"]
        return e.get("name") or e.get("title") or ""
    except Exception:
        return ""


def fetch_embed(kind, lid):
    status, html = http_get(f"https://open.spotify.com/embed/{kind}/{lid}",
                            headers={"User-Agent": UA_BROWSER, "Accept-Language": "ja,en;q=0.8"},
                            accept="text/html")
    if status == 404 or not html:
        raise UserError("プレイリストが見つかりませんでした。公開されているプレイリストか確認してください。")
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise UserError("プレイリストの読み込みに失敗しました（Spotify のページ形式が変わった可能性があります）。"
                        "曲を全選択してコピーしたリンクを貼り付ける方法なら確実に読み込めます。")
    data = json.loads(m.group(1))
    tl = find_track_list(data) or []
    tracks = []
    for t in tl:
        mm = re.match(r"spotify:track:([A-Za-z0-9]{22})", str(t.get("uri", "")))
        if not mm:
            continue
        artists = t.get("subtitle") or ""
        if isinstance(artists, list):
            artists = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in artists)
        tracks.append({"id": mm.group(1), "title": t.get("title") or t.get("name") or "",
                       "artists": [a.strip() for a in str(artists).split(",") if a.strip()]})
    if not tracks:
        raise UserError("プレイリストから曲を読み取れませんでした。曲を全選択してコピーしたリンクを貼り付けてください。")
    return find_entity_name(data), tracks


def parse_input(text, log):
    """URL・曲リンクのまじった入力から曲リストを作る"""
    tracks, seen, names = [], set(), []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for kind, lid in LIST_RE.findall(line):
            log(f"{'プレイリスト' if kind == 'playlist' else 'アルバム'}を読み込み中…")
            name, tl = fetch_embed(kind, lid)
            if name:
                names.append(name)
            for t in tl:
                if t["id"] not in seen:
                    seen.add(t["id"]); tracks.append(t)
            if len(tl) >= 100:
                log("注意: 埋め込みページからは最大100曲程度しか読み込めないことがあります。"
                    "足りない場合は、曲を全選択してコピーしたリンクも貼り付けてください。")
        ids = TRACK_RE.findall(line)
        if not ids and BARE_RE.match(line):
            ids = [line]
        for sid in ids:
            if sid not in seen:
                seen.add(sid); tracks.append({"id": sid, "title": "", "artists": []})
    return " / ".join(names), tracks


# ================================================================ sources

def fetch_recco(ids, prog):
    """ReccoBeats: 曲情報は Spotify ID で引けるが、特徴量の返り値は ReccoBeats 独自のIDのことがあるので対応表を作る"""
    meta, feats, todo = {}, {}, []
    for sid in ids:
        c = CACHE.get("recco2", sid)
        if c:
            meta[sid] = c.get("meta") or {}
            if c.get("feat"):
                feats[sid] = c["feat"]
        else:
            todo.append(sid)
    raw_meta, raw_feat, uuid2sid, sid2uuid = {}, {}, {}, {}
    for n in range(0, len(todo), BATCH):
        chunk = todo[n:n + BATCH]
        prog("ReccoBeats", n, len(todo))
        data = get_json(f"{RECCO}/track?ids={','.join(chunk)}")
        for it in (data or {}).get("content", []) if isinstance(data, dict) else []:
            sid = spotify_id_of(it)
            if sid:
                raw_meta[sid] = it
                if it.get("id"):
                    uuid2sid[it["id"]] = sid
                    sid2uuid[sid] = it["id"]
        data = get_json(f"{RECCO}/audio-features?ids={','.join(chunk)}")
        items = (data or {}).get("content", []) if isinstance(data, dict) else []
        for pos, it in enumerate(items):
            sid = spotify_id_of(it) or uuid2sid.get(it.get("id"))
            if sid:
                raw_feat[sid] = it
        time.sleep(0.3)
    missing = [i for i in todo if i not in raw_feat]
    for j, sid in enumerate(missing):
        prog("ReccoBeats（個別）", j, len(missing))
        for ident in [x for x in (sid2uuid.get(sid), sid) if x]:
            d = get_json(f"{RECCO}/track/{ident}/audio-features")
            if isinstance(d, dict) and "tempo" in d:
                raw_feat[sid] = d
                break
        time.sleep(0.2)
    out = {}
    for sid in ids:
        if sid in todo:
            t, f = raw_meta.get(sid) or {}, raw_feat.get(sid) or {}
            m = {"title": t.get("trackTitle") or t.get("title") or t.get("name") or "",
                 "artists": [a.get("name", "") for a in (t.get("artists") or []) if isinstance(a, dict)]}
            key, mode = f.get("key"), f.get("mode")
            feat = None
            if num(f.get("tempo")) or (isinstance(key, int) and 0 <= key <= 11):
                feat = {"bpm": num(f.get("tempo")),
                        "key": key if isinstance(key, int) and 0 <= key <= 11 else None,
                        "mode": mode if mode in (0, 1) else None}
            if t or f:  # 何も返らなかった曲はキャッシュしない（次回また探す）
                CACHE.put("recco2", sid, {"meta": m, "feat": feat})
        else:
            m, feat = meta.get(sid) or {"title": "", "artists": []}, feats.get(sid)
        out[sid] = {"meta": m, "feat": feat}
    prog("ReccoBeats", len(todo), len(todo))
    return out


def fetch_oembed_title(sid):
    c = CACHE.get("oembed", sid)
    if c:
        return c.get("title", "")
    try:
        d = get_json("https://open.spotify.com/oembed?url=" +
                     urllib.parse.quote(f"https://open.spotify.com/track/{sid}", safe="")) or {}
    except Exception:
        return ""
    title = d.get("title", "") if isinstance(d, dict) else ""
    CACHE.put("oembed", sid, {"title": title})
    return title


def pick_gsbpm(results, title, artists):
    nt = norm(title)
    nas = [norm(a) for a in artists if a]
    best, best_score = None, 0
    for r in results:
        if not isinstance(r, dict):
            continue
        rt = norm(r.get("title", ""))
        art = r.get("artist")
        ra = norm(art.get("name", "") if isinstance(art, dict) else "")
        if not rt:
            continue
        ts = 3 if rt == nt else (1 if nt and (nt in rt or rt in nt) else 0)
        if ts == 0:
            continue
        asc = 0
        if nas:
            asc = 3 if ra in nas else (2 if any(a and ra and (a in ra or ra in a) for a in nas) else 0)
            if asc == 0:
                continue
        if ts + asc > best_score:
            best, best_score = r, ts + asc
    if not nas:
        exact = [r for r in results if isinstance(r, dict) and norm(r.get("title", "")) == nt]
        return (exact[0], "title-only") if len(exact) == 1 else (None, "")
    return best, (("exact" if best_score >= 6 else "fuzzy") if best else "")


def fetch_gsbpm(ids, info, key, prog):
    out = {}
    q = urllib.parse.quote
    for i, sid in enumerate(ids):
        prog("GetSongBPM", i, len(ids))
        c = CACHE.get("gsbpm", sid)
        if c:
            out[sid] = None if c.get("none") else clean(c)
            continue
        title, artists = info[sid]["title"], info[sid]["artists"]
        if not title:
            out[sid] = None
            continue
        lookup = f"song:{title}" + (f" artist:{artists[0]}" if artists else "")
        data = get_json(f"{GSBPM}/search/?api_key={q(key)}&type=both&limit=15&lookup={q(lookup)}")
        results = data.get("search") if isinstance(data, dict) else None
        if not isinstance(results, list) and artists:
            time.sleep(0.4)
            data = get_json(f"{GSBPM}/search/?api_key={q(key)}&type=song&limit=25&lookup={q(title)}")
            results = data.get("search") if isinstance(data, dict) else None
        hit, how = pick_gsbpm(results if isinstance(results, list) else [], title, artists)
        res = None
        if hit:
            song = hit
            if not (hit.get("tempo") and hit.get("key_of")) and hit.get("id"):
                time.sleep(0.4)
                d = get_json(f"{GSBPM}/song/?api_key={q(key)}&id={q(str(hit['id']))}")
                song = (d or {}).get("song") or hit
            k, m = parse_key_name(song.get("key_of"))
            art = song.get("artist") if isinstance(song.get("artist"), dict) else {}
            res = {"bpm": num(song.get("tempo")), "key": k, "mode": m, "url": song.get("uri") or "",
                   "match": how, "matched": f"{song.get('title', '')} / {art.get('name', '')}"}
            if res["bpm"] is None and res["key"] is None:
                res = None
        CACHE.put("gsbpm", sid, res or {"none": True})
        out[sid] = res
        time.sleep(0.4)
    prog("GetSongBPM", len(ids), len(ids))
    return out


def find_features(d):
    if not isinstance(d, dict):
        return None
    for cand in (d, d.get("features"), d.get("audio_features"), d.get("data")):
        if isinstance(cand, dict) and ("tempo" in cand or "key" in cand):
            return cand
    return None


def fetch_soundstat(ids, key, prog):
    out = {}
    for i, sid in enumerate(ids):
        prog("SoundStat", i, len(ids))
        c = CACHE.get("soundstat", sid)
        if c:
            out[sid] = None if c.get("none") else clean(c)
            continue
        d = get_json(f"{SOUNDSTAT}/track/{sid}", headers={"x-api-key": key})
        if isinstance(d, dict) and (d.get("__pending__") or
                                    str(d.get("status", "")).lower() in ("processing", "pending", "queued")):
            out[sid] = {"pending": True}
            continue
        f = find_features(d)
        res = None
        if f:
            k, m = f.get("key"), f.get("mode")
            if isinstance(k, str):
                k, m = parse_key_name(k)
            res = {"bpm": num(f.get("tempo")),
                   "key": k if isinstance(k, int) and 0 <= k <= 11 else None,
                   "mode": int(m) if m in (0, 1, "0", "1") else None}
            if isinstance(f.get("key_confidence"), (int, float)):
                res["key_conf"] = round(f["key_confidence"], 2)
            if res["bpm"] is None and res["key"] is None:
                res = None
        CACHE.put("soundstat", sid, res or {"none": True})
        out[sid] = res
        time.sleep(0.3)
    prog("SoundStat", len(ids), len(ids))
    return out


# ================================================================ analysis job

JOBS = {}
JOB_LOCK = threading.Lock()


def analyze(job, text, refresh):
    def log(msg):
        job["log"].append(msg)

    def prog(stage, done, total):
        job["stage"], job["done"], job["total"] = stage, done, total

    settings = load_settings()
    gs_key = (settings.get("getsongbpm_api_key") or "").strip()
    ss_key = (settings.get("soundstat_api_key") or "").strip()

    prog("曲リストを読み込み中", 0, 0)
    name, tracks = parse_input(text, log)
    if not tracks:
        raise UserError("Spotify のプレイリストURL、または曲のリンクが見つかりませんでした。")
    ids = [t["id"] for t in tracks]
    if refresh:
        CACHE.drop(ids)
    log(f"{len(ids)} 曲を読み込みました。")

    recco = fetch_recco(ids, prog)
    info = {}
    for i, t in enumerate(tracks):
        m = recco[t["id"]]["meta"]
        title = t["title"] or m.get("title") or ""
        artists = t["artists"] or m.get("artists") or []
        if not title:
            prog("曲名を確認中", i, len(tracks))
            title = fetch_oembed_title(t["id"])
        info[t["id"]] = {"title": title, "artists": artists}

    sources = ["recco"]  # 画面側で GetSongBPM を先頭（標準）に並べ替える
    gs = ss = None
    for label, enabled, fn, args in (("GetSongBPM", gs_key, fetch_gsbpm, (ids, info, gs_key, prog)),
                                     ("SoundStat", ss_key, fetch_soundstat, (ids, ss_key, prog))):
        if not enabled:
            continue
        try:
            res = fn(*args)
            if label == "GetSongBPM":
                gs = res; sources.append("gsbpm")
            else:
                ss = res; sources.append("soundstat")
        except AuthError:
            log(f"{label}: APIキーが無効か、利用が許可されていません。設定を確認してください。")
        except UserError:
            raise
        except Exception as e:
            log(f"{label}: 取得に失敗しました（{e}）。この照合先は使わずに続けます。")
        CACHE.save()
    CACHE.save()
    if not gs_key:
        log("GetSongBPM のAPIキーが未設定のため、ReccoBeats の値だけで解析しました（右上の「設定」から登録できます）。")

    rows = []
    for sid in ids:
        src = {"recco": recco[sid]["feat"]}
        if gs is not None:
            src["gsbpm"] = gs.get(sid)
        if ss is not None:
            src["soundstat"] = ss.get(sid)
        rows.append({"id": sid, "title": info[sid]["title"], "artist": ", ".join(info[sid]["artists"]), "src": src})

    payload = {"id": time.strftime("%Y%m%d-%H%M%S"), "title": name or f"{len(rows)} 曲",
               "generated": time.strftime("%Y-%m-%d %H:%M"), "sources": sources, "rows": rows,
               "input": text.strip()[:2000]}
    save_json(HISTORY_DIR / f"{payload['id']}.json", payload)
    return payload


def run_job(job, text, refresh):
    try:
        job["result"] = analyze(job, text, refresh)
        job["status"] = "done"
    except UserError as e:
        job["status"], job["error"] = "error", str(e)
    except Exception as e:
        job["status"], job["error"] = "error", f"予期しないエラーが発生しました: {e}"
    finally:
        try:
            CACHE.save()
        except Exception:
            pass


def list_history():
    items = []
    for p in sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:30]:
        d = load_json(p, None)
        if d:
            items.append({"id": d.get("id", p.stem), "title": d.get("title", ""), "generated": d.get("generated", ""),
                          "count": len(d.get("rows", []))})
    return items


def demo_payload():
    import random
    random.seed(11)
    names = [("夜光", "サンプル A"), ("Glass Run", "サンプル B"), ("回送列車", "サンプル C"), ("Low Tide", "サンプル D"),
             ("7/8 Drift", "サンプル E"), ("白昼夢", "サンプル F"), ("Neon Rain", "サンプル G"), ("Half Step", "サンプル H"),
             ("Orbit", "サンプル I"), ("Paper Moon", "サンプル J"), ("終電", "サンプル K"), ("Signal", "サンプル L"),
             ("データなしの例", "サンプル M")]
    rows = []
    for i, (t, a) in enumerate(names):
        bpm = round(random.choice([87, 120, 124, 126, 128, 128, 140, 174]) + random.uniform(-1, 1), 2)
        k, m = random.choice([9, 9, 0, 7, 2, 4, 5]), random.randint(0, 1)
        rows.append({"id": f"demo{i:018d}", "title": t, "artist": a,
                     "src": {"recco": {"bpm": bpm, "key": k, "mode": m},
                             "gsbpm": {"bpm": round(bpm), "key": k, "mode": m, "match": "exact", "matched": f"{t} / {a}"}}})
    rows[2]["src"]["gsbpm"]["bpm"] = round(rows[2]["src"]["recco"]["bpm"] / 2)
    k, m = rows[4]["src"]["recco"]["key"], rows[4]["src"]["recco"]["mode"]
    rows[4]["src"]["gsbpm"].update({"key": (k + 3) % 12 if m == 0 else (k + 9) % 12, "mode": 1 - m})
    rows[6]["src"]["gsbpm"]["key"] = (rows[6]["src"]["recco"]["key"] + 5) % 12
    rows[7]["src"]["gsbpm"] = None
    rows[-1]["src"] = {"recco": None, "gsbpm": None}
    return {"id": "demo", "title": "サンプルデータ", "generated": time.strftime("%Y-%m-%d %H:%M"),
            "sources": ["recco", "gsbpm"], "rows": rows, "demo": True}


# ================================================================ server

class Handler(BaseHTTPRequestHandler):
    server_version = "KeyBPM/3"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if not isinstance(body, str) else body.encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _local_only(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, APP_HTML, "text/html; charset=utf-8")
        if path == "/api/ping":
            return self._send(200, {"app": "key-bpm", "ok": True})
        if path == "/api/settings":
            s = load_settings()
            return self._send(200, {"getsongbpm": bool(s.get("getsongbpm_api_key")),
                                    "soundstat": bool(s.get("soundstat_api_key"))})
        if path == "/api/history":
            return self._send(200, list_history())
        if path.startswith("/api/history/"):
            hid = re.sub(r"[^\w\-]", "", path.rsplit("/", 1)[-1])
            if hid == "demo":
                return self._send(200, demo_payload())
            d = load_json(HISTORY_DIR / f"{hid}.json", None)
            return self._send(200, d) if d else self._send(404, {"error": "見つかりませんでした"})
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._send(404, {"error": "not found"})
            return self._send(200, {k: job.get(k) for k in ("status", "stage", "done", "total", "log", "error", "result")})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._local_only():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        if path == "/api/analyze":
            with JOB_LOCK:
                if any(j["status"] == "running" for j in JOBS.values()):
                    return self._send(409, {"error": "別の解析が実行中です。終わるまでお待ちください。"})
                jid = uuid.uuid4().hex[:12]
                job = {"status": "running", "stage": "準備中", "done": 0, "total": 0, "log": [], "error": None, "result": None}
                JOBS[jid] = job
            threading.Thread(target=run_job, args=(job, body.get("text", ""), bool(body.get("refresh"))), daemon=True).start()
            return self._send(200, {"job": jid})
        if path == "/api/settings":
            s = load_settings()
            for k in ("getsongbpm_api_key", "soundstat_api_key"):
                if k in body:
                    v = str(body[k] or "").strip()
                    if v:
                        s[k] = v
                    else:
                        s.pop(k, None)
            save_json(CONFIG, s)
            return self._send(200, {"ok": True})
        if path == "/api/history/delete":
            hid = re.sub(r"[^\w\-]", "", str(body.get("id", "")))
            p = HISTORY_DIR / f"{hid}.json"
            if hid and p.exists():
                p.unlink()
            return self._send(200, {"ok": True})
        if path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        self._send(404, {"error": "not found"})


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "key-bpm"
    except Exception:
        return False


def free_port(start):
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("使えるポートが見つかりませんでした。")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    no_browser = "--no-browser" in sys.argv
    if already_running(PORT):
        print(f"{APP_NAME} はすでに起動しています。ブラウザで開きます。")
        if not no_browser:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return
    port = free_port(PORT)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"{APP_NAME} を起動しました: {url}")
    print("このウィンドウを閉じるか、画面右上の「終了」を押すと止まります。")
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


APP_HTML = r'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>キー/BPM 照合ツール</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+JP:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {
  /* layout: 上部バー → 入力カード → 結果（タブ: マッシュアップ候補 / 基準曲と比較） */
  --bg: #f3f4f1;
  --surface: #ffffff;
  --ink: #1d2124;
  --muted: #6a7178;
  --line: #dfe2dd;
  --accent: #3446d6;
  --accent-soft: #e6e9fb;
  --good: #1f8a4c;  --good-bg: #e2f3e8;
  --warn: #a8670a;  --warn-bg: #fbf0dc;
  --far: #b23a3a;   --far-bg: #f8e4e2;
  --font-body: "IBM Plex Sans JP", "Hiragino Sans", "Yu Gothic UI", system-ui, sans-serif;
  --font-num: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #15181b; --surface: #1d2125; --ink: #e7e9e6; --muted: #959ca3; --line: #2f343a;
    --accent: #8d9bff; --accent-soft: #262c52;
    --good: #5cc98a; --good-bg: #183324; --warn: #e3a94b; --warn-bg: #3a2c14; --far: #ee8a84; --far-bg: #3c1f1e;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #15181b; --surface: #1d2125; --ink: #e7e9e6; --muted: #959ca3; --line: #2f343a;
  --accent: #8d9bff; --accent-soft: #262c52;
  --good: #5cc98a; --good-bg: #183324; --warn: #e3a94b; --warn-bg: #3a2c14; --far: #ee8a84; --far-bg: #3c1f1e;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--ink); font: 14px/1.55 var(--font-body); }
.num { font-family: var(--font-num); font-variant-numeric: tabular-nums; }
[hidden] { display: none !important; }

/* top bar */
.bar { position: sticky; top: 0; z-index: 5; background: var(--surface); border-bottom: 1px solid var(--line); }
.bar-in { max-width: 1280px; margin: 0 auto; padding: 10px 20px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.brand { font-weight: 700; font-size: 15px; display: flex; align-items: center; gap: 8px; }
.brand-mark { width: 22px; height: 22px; border-radius: 6px; background: var(--accent); display: grid; place-items: center; color: #fff; font: 500 11px var(--font-num); }
.bar .spacer { flex: 1; }

.wrap { max-width: 1280px; margin: 0 auto; padding: 20px 20px 60px; display: grid; gap: 18px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
h1 { font-size: 19px; margin: 0; text-wrap: balance; }
h2 { font-size: 15px; margin: 0 0 8px; }
.sub { color: var(--muted); font-size: 12.5px; }

/* controls */
input[type="search"], input[type="number"], input[type="text"], input[type="password"], select, textarea {
  font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 6px 9px;
}
textarea { width: 100%; resize: vertical; min-height: 64px; font-family: var(--font-num); font-size: 13px; }
input[type="number"] { width: 70px; }
button {
  font: inherit; font-size: 13px; cursor: pointer; border-radius: 6px; padding: 6px 12px;
  border: 1px solid var(--line); background: var(--surface); color: var(--ink);
}
button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
button:disabled { opacity: .5; cursor: default; }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 500; }
button.primary:hover:not(:disabled) { filter: brightness(1.08); color: #fff; }
button.link { border: 0; background: none; color: var(--accent); padding: 0; text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.row { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center; }
.row label { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }

/* input card */
.input-card { display: grid; gap: 10px; }
.steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 4px; }
.step { border: 1px dashed var(--line); border-radius: 8px; padding: 10px 12px; font-size: 13px; color: var(--muted); }
.step b { color: var(--ink); display: block; font-size: 13px; }
.progress { display: grid; gap: 6px; }
.pbar { height: 6px; background: var(--line); border-radius: 99px; overflow: hidden; }
.pbar i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .3s; }
.pbar.indet i { width: 30%; animation: slide 1.2s infinite ease-in-out; }
@keyframes slide { 0% { margin-left: -30%; } 100% { margin-left: 100%; } }
.log { font-size: 12.5px; color: var(--muted); display: grid; gap: 2px; }
.notice { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; background: var(--warn-bg); color: var(--ink); border-radius: 8px; padding: 10px 12px; font-size: 13px; }
.notice span { color: var(--muted); flex: 1 1 320px; }
.err { background: var(--far-bg); color: var(--far); border-radius: 8px; padding: 10px 12px; font-size: 13px; }

/* tabs */
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); }
.tab { border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: none; padding: 8px 14px; font-weight: 500; color: var(--muted); }
.tab[aria-selected="true"] { color: var(--accent); border-bottom-color: var(--accent); }
.tab .count { font: 500 11px var(--font-num); background: var(--warn-bg); color: var(--warn); border-radius: 99px; padding: 1px 7px; margin-left: 6px; }

/* stats */
.top { display: grid; grid-template-columns: minmax(280px, 1.4fr) repeat(4, minmax(120px, 1fr)); gap: 12px; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
.ref { border-left: 4px solid var(--accent); }
.label { font-size: 11.5px; color: var(--muted); letter-spacing: .04em; }
.ref .t { font-size: 17px; font-weight: 700; margin: 2px 0 0; overflow-wrap: anywhere; }
.ref .a { color: var(--muted); font-size: 13px; }
.ref .facts { display: flex; gap: 18px; margin-top: 10px; flex-wrap: wrap; }
.ref .facts b { display: block; font-size: 20px; font-family: var(--font-num); font-weight: 500; }
.stat b { display: block; font: 500 26px/1.2 var(--font-num); margin-top: 4px; }
.stat small { color: var(--muted); font-size: 12px; }
.stat.alert { border-top: 3px solid var(--warn); }

/* tables */
.tablebox { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
#refTable { min-width: 1400px; }
#pairTable { min-width: 980px; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }
th { font-size: 12px; font-weight: 500; color: var(--muted); white-space: nowrap; background: var(--surface); }
th[data-sort] { cursor: pointer; user-select: none; }
th[data-sort]:hover { color: var(--accent); }
th .arrow { font-size: 10px; margin-left: 3px; }
th.r, td.r { text-align: right; }
tr:last-child td { border-bottom: 0; }
tr.is-ref td { background: var(--accent-soft); }
td.song { min-width: 200px; }
.song b { font-weight: 500; display: block; overflow-wrap: anywhere; }
.song span { color: var(--muted); font-size: 12px; }
.song .vals { font: 12px var(--font-num); color: var(--ink); margin-top: 2px; display: block; }
.song a { color: inherit; text-decoration: none; }
.song a:hover b { color: var(--accent); text-decoration: underline; }
td input.bpm { width: 78px; text-align: right; font-family: var(--font-num); padding: 3px 6px; }
td select.key { padding: 3px 4px; font-size: 13px; }
.edited { border-color: var(--warn) !important; }
.chip { display: inline-block; border-radius: 999px; padding: 1px 9px; font-size: 12px; white-space: nowrap; }
.chip.good { background: var(--good-bg); color: var(--good); }
.chip.warn { background: var(--warn-bg); color: var(--warn); }
.chip.far  { background: var(--far-bg);  color: var(--far); }
.chip.none { color: var(--muted); border: 1px dashed var(--line); }
.chip.manual { background: var(--accent-soft); color: var(--accent); }
.tag { font-size: 11px; color: var(--muted); margin-left: 4px; }
.zero { color: var(--good); font-weight: 500; }
.badge-ref { font-size: 11px; color: var(--accent); font-weight: 700; }
.srcs, .reasons, .stack { display: flex; flex-direction: column; gap: 3px; align-items: flex-start; }
.src { font: 12px/1.3 var(--font-num); padding: 2px 7px; border-radius: 5px; border: 1px solid var(--line); background: transparent; color: var(--ink); white-space: nowrap; text-align: left; }
.src .n { color: var(--muted); font-family: var(--font-body); font-size: 11px; margin-right: 5px; }
.src.on { border-color: var(--accent); background: var(--accent-soft); }
.src.empty { color: var(--muted); border-style: dashed; cursor: default; }
.src.empty:hover { color: var(--muted); border-color: var(--line); }
.small { font-size: 12px; color: var(--muted); }
.score { display: flex; align-items: center; gap: 8px; }
.score i { display: block; width: 46px; height: 5px; border-radius: 99px; background: var(--line); overflow: hidden; }
.score i b { display: block; height: 100%; background: var(--good); }
.pair-arrow { color: var(--muted); font-size: 12px; }

details.help { font-size: 13px; color: var(--muted); }
details.help summary { cursor: pointer; color: var(--ink); }
details.help ul { margin: 8px 0 0; padding-left: 20px; max-width: 78ch; }

dialog { border: 1px solid var(--line); border-radius: 12px; padding: 0; background: var(--surface); color: var(--ink); width: min(520px, calc(100% - 32px)); }
dialog::backdrop { background: rgba(0,0,0,.35); }
.dlg { padding: 18px 20px; display: grid; gap: 14px; }
.field { display: grid; gap: 4px; }
.field label { font-weight: 500; font-size: 13px; display: flex; gap: 8px; align-items: center; }
.field input { width: 100%; font-family: var(--font-num); }
.field p { margin: 0; font-size: 12px; color: var(--muted); }
.field a { color: var(--accent); }
.dlg-actions { display: flex; justify-content: flex-end; gap: 8px; }

.foot { max-width: 1280px; margin: 0 auto; padding: 0 20px 28px; font-size: 12px; color: var(--muted); }
.foot a { color: var(--muted); }
.toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--ink); color: var(--bg); padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 10; }
.toast.show { opacity: 1; }
@media (prefers-reduced-motion: reduce) { .toast, .pbar i { transition: none; } .pbar.indet i { animation: none; width: 100%; opacity: .5; } }
@media (max-width: 980px) { .top { grid-template-columns: 1fr 1fr; } .ref { grid-column: 1 / -1; } .steps { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header class="bar">
  <div class="bar-in">
    <div class="brand"><span class="brand-mark" aria-hidden="true">♯</span>キー/BPM 照合ツール</div>
    <span class="spacer"></span>
    <label for="history" class="small">解析履歴
      <select id="history"><option value="">履歴はまだありません</option></select>
    </label>
    <button type="button" id="openSettings">設定</button>
    <button type="button" id="quit">終了</button>
  </div>
</header>

<main class="wrap">
  <section class="card input-card" aria-label="プレイリストの読み込み">
    <label for="input"><h2 style="margin:0">Spotify のプレイリストURLを貼り付け</h2></label>
    <textarea id="input" rows="2" placeholder="https://open.spotify.com/playlist/…（曲のリンクやアルバムURLも貼り付けられます。複数行OK）"></textarea>
    <div class="row">
      <button type="button" class="primary" id="analyze">解析する</button>
      <label for="refresh"><input type="checkbox" id="refresh"> 保存済みの結果を使わずに取り直す</label>
      <span class="small" id="srcNote"></span>
    </div>
    <div class="notice" id="keyNotice" hidden>
      <b>GetSongBPM の APIキーを設定してください</b>
      <span>BPM の取得は GetSongBPM が標準です。キーが未設定のあいだは ReccoBeats の値だけで解析します。</span>
      <button type="button" class="primary" id="keyNoticeBtn">APIキーを設定</button>
    </div>
    <div class="progress" id="progress" hidden>
      <div class="pbar" id="pbar"><i></i></div>
      <div class="small" id="stage"></div>
    </div>
    <div class="log" id="log"></div>
    <div class="err" id="error" hidden></div>
    <div class="steps" id="steps">
      <div class="step"><b>1. URLを貼って「解析する」</b>Spotify でプレイリストの「共有 → リンクをコピー」で取れるURLです。</div>
      <div class="step"><b>2. マッシュアップ候補を見る</b>BPM とキーが近い組み合わせが、相性の良い順に並びます。</div>
      <div class="step"><b>3. 「要確認」を購入前にチェック</b>データベースどうしで値がずれている曲に印が付きます。 <button type="button" class="link" id="demo">サンプルで試す</button></div>
    </div>
  </section>

  <section id="result" hidden>
    <div style="display:grid; gap:14px">
      <header>
        <h1 id="resTitle"></h1>
        <div class="sub" id="resMeta"></div>
      </header>
      <div class="tabs" role="tablist">
        <button type="button" class="tab" role="tab" id="tabPairs" aria-selected="true" aria-controls="panelPairs">マッシュアップ候補</button>
        <button type="button" class="tab" role="tab" id="tabRef" aria-selected="false" aria-controls="panelRef">基準曲と比較<span class="count" id="checkCount" hidden></span></button>
      </div>

      <!-- マッシュアップ候補 -->
      <div id="panelPairs" role="tabpanel" aria-labelledby="tabPairs" style="display:grid; gap:14px">
        <div class="row">
          <label for="pFocus">曲で絞る
            <select id="pFocus"><option value="">すべての組み合わせ</option></select>
          </label>
          <label for="pTol">BPM差 ±<input id="pTol" type="number" min="0" max="50" step="0.5" value="8">%</label>
          <label for="pShift">移調は最大
            <select id="pShift"><option value="0">しない</option><option value="1">±1半音</option><option value="2" selected>±2半音</option><option value="3">±3半音</option><option value="4">±4半音</option></select>
          </label>
          <label for="pHd"><input id="pHd" type="checkbox" checked> ハーフ/ダブルタイムも候補に</label>
          <label for="pVerified"><input id="pVerified" type="checkbox"> 照合で一致した曲だけ</label>
          <label for="pSort">並び順
            <select id="pSort"><option value="score">おすすめ順</option><option value="bpm">BPMが近い順</option><option value="key">移調が少ない順</option></select>
          </label>
        </div>
        <div class="small" id="pSummary"></div>
        <div class="tablebox">
          <table id="pairTable">
            <thead><tr>
              <th class="r">#</th><th>曲 A</th><th></th><th>曲 B</th><th>テンポ</th><th>キー</th><th>照合</th><th>相性</th><th></th>
            </tr></thead>
            <tbody id="pairBody"></tbody>
          </table>
        </div>
        <details class="help">
          <summary>マッシュアップ候補の見かた</summary>
          <ul>
            <li><b>テンポ</b>：曲Bを曲Aのテンポに合わせるときの伸縮率。「×2」「÷2」は倍・半分のテンポとして合わせる組み合わせです。</li>
            <li><b>キー</b>：「移調不要」は同じキー・平行調・Camelotで隣のキーなど、そのまま重ねやすい組み合わせ。「Bを+2」は曲Bを2半音上げると同じキーになるという意味です。</li>
            <li><b>照合</b>：どちらかの曲で、データベースどうしの値がずれていると「要確認」になります。購入前に「基準曲と比較」タブで値を確かめてください。</li>
            <li><b>相性</b>：テンポの伸縮率と移調量が小さいほど高くなります。</li>
          </ul>
        </details>
      </div>

      <!-- 基準曲と比較 -->
      <div id="panelRef" role="tabpanel" aria-labelledby="tabRef" hidden style="display:grid; gap:14px">
        <section class="top" aria-label="基準曲と集計">
          <div class="panel ref" id="refPanel"></div>
          <div class="panel stat"><div class="label">BPMが近い</div><b id="sBpm" class="num">–</b><small id="sBpmNote"></small></div>
          <div class="panel stat"><div class="label">キーの相性が良い</div><b id="sKey" class="num">–</b><small>同じ・平行調・Camelot隣接</small></div>
          <div class="panel stat"><div class="label">どちらも近い</div><b id="sBoth" class="num">–</b><small>マッシュアップ候補</small></div>
          <div class="panel stat alert"><div class="label">要確認</div><b id="sCheck" class="num">–</b><small id="sCheckNote">データベース間でずれ</small></div>
        </section>
        <div class="row">
          <label for="q">検索 <input id="q" type="search" placeholder="曲名・アーティスト"></label>
          <label for="tol">BPM許容差 ±<input id="tol" type="number" min="0" max="50" step="0.5" value="6">%</label>
          <label for="hd"><input id="hd" type="checkbox" checked> ハーフ/ダブルタイムも考慮</label>
          <label for="show">表示
            <select id="show">
              <option value="all">すべて</option>
              <option value="bpm">BPMが近い曲</option>
              <option value="key">キーの相性が良い曲</option>
              <option value="both">どちらも近い曲</option>
              <option value="check">要確認の曲</option>
            </select>
          </label>
          <span class="spacer" style="flex:1"></span>
          <button id="resetEdits" type="button">手入力をリセット</button>
          <button id="csv" type="button">CSVで保存</button>
        </div>
        <div class="tablebox">
          <table id="refTable">
            <thead>
              <tr>
                <th data-sort="idx" class="r">#</th>
                <th data-sort="title">曲</th>
                <th data-sort="bpm" class="r">BPM</th>
                <th data-sort="keyIdx">キー</th>
                <th data-sort="checkRank" title="複数のデータベースの値が一致しているか">照合</th>
                <th title="各データベースの値。クリックするとその値を採用します">データベースの値</th>
                <th data-sort="camelotSort">Camelot</th>
                <th data-sort="bpmDiffAbs" class="r">BPM差</th>
                <th data-sort="bpmPctAbs" class="r">BPM差 %</th>
                <th data-sort="keyShiftAbs" class="r" title="この曲を基準曲のキーに合わせるのに必要な移調量（半音）">キー差（半音）</th>
                <th data-sort="compatRank">キーの相性</th>
                <th data-sort="afterAbs" class="r" title="キーロックなしでテンポを基準曲に合わせたとき、残るキー差（半音）">テンポ合わせ後のキー差</th>
                <th>基準</th>
              </tr>
            </thead>
            <tbody id="tbody"></tbody>
          </table>
        </div>
        <details class="help">
          <summary>各列の見かた</summary>
          <ul>
            <li><b>照合</b>：複数のデータベースの値を比べた結果です。BPMの倍/半分のずれと、平行調（例：Aマイナー⇔Cメジャー）の取り違えはよくあるので個別に表示します。</li>
            <li><b>データベースの値</b>：RB＝ReccoBeats、GS＝GetSongBPM、SS＝SoundStat。値をクリックするとその値を採用します。初期値は GetSongBPM の値です。GetSongBPM に見つからない曲は ReccoBeats の値を使います。3つのデータベースを使っている場合は、2つ以上で一致した値を優先します。</li>
            <li><b>キー差（半音）</b>：この曲を何半音動かせば基準曲と同じキーになるか。長調と短調が違う場合は平行調として比べます。</li>
            <li><b>キーの相性</b>：Camelotホイール上の距離。同じ番号（同じキー・平行調）と ±1 は自然につながりやすく、±2 は工夫が必要です。</li>
            <li><b>テンポ合わせ後のキー差</b>：キーロックなしで再生速度を変えると音程も動くため、その分を差し引いた残りのキー差です。</li>
            <li>BPMやキーは表の中で直接書き換えられます（このパソコンのブラウザに保存されます）。</li>
          </ul>
        </details>
      </div>
    </div>
  </section>
</main>

<footer class="foot">
  BPM・キーのデータ提供：<a href="https://getsongbpm.com" target="_blank" rel="noopener">GetSongBPM.com</a> ・
  <a href="https://reccobeats.com" target="_blank" rel="noopener">ReccoBeats</a> ・
  <a href="https://github.com/maigohasegawa/key-bpm-tool" target="_blank" rel="noopener">このツールについて</a>
</footer>

<dialog id="settings" aria-labelledby="settingsTitle">
  <form class="dlg" id="settingsForm" method="dialog">
    <h2 id="settingsTitle" style="margin:0">照合に使うデータベース</h2>
    <p class="small" style="margin:0">BPM・キーは GetSongBPM の値を標準で使い、見つからない曲は ReccoBeats（設定不要）で補います。</p>
    <div class="field">
      <label for="gsKey">GetSongBPM の APIキー（標準） <span class="chip good" id="gsSet" hidden>設定済み</span></label>
      <input id="gsKey" type="password" autocomplete="off" placeholder="未設定（空のまま保存すると変更しません）">
      <p>無料。<a href="https://getsongbpm.com/api" target="_blank" rel="noopener">getsongbpm.com/api</a> で取得できます。利用条件として、自分のサイト等に GetSongBPM.com へのリンクを置く必要があります。</p>
    </div>
    <div class="field">
      <label for="ssKey">SoundStat の APIキー <span class="chip good" id="ssSet" hidden>設定済み</span></label>
      <input id="ssKey" type="password" autocomplete="off" placeholder="未設定（任意）">
      <p>任意。<a href="https://soundstat.info" target="_blank" rel="noopener">soundstat.info</a> でキーを取得した場合のみ。</p>
    </div>
    <label for="showKeys" class="small" style="display:flex; gap:6px; align-items:center"><input type="checkbox" id="showKeys"> キーを表示する</label>
    <div class="dlg-actions">
      <button type="button" id="clearKeys">キーを削除</button>
      <span style="flex:1"></span>
      <button type="button" id="cancelSettings">キャンセル</button>
      <button type="button" class="primary" id="saveSettings">保存</button>
    </div>
  </form>
</dialog>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const NOTES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
const NOTES_FLAT = ["C","D♭","D","E♭","E","F","G♭","G","A♭","A","B♭","B"];
const SRC_LABEL = { recco: ["RB", "ReccoBeats"], gsbpm: ["GS", "GetSongBPM"], soundstat: ["SS", "SoundStat"] };
const STORE_KEY = "kbc-edits-v2";
// 優先順: GetSongBPM を標準にし、見つからない曲は ReccoBeats → SoundStat の値を使う
const PRIORITY = ["gsbpm", "recco", "soundstat"];
const mod = (n, m) => ((n % m) + m) % m;
const signed = d => { d = mod(d, 12); return d > 6 ? d - 12 : d; };

let edits = {};
try { edits = JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {}; } catch (e) { edits = {}; }
function saveEdits() { try { localStorage.setItem(STORE_KEY, JSON.stringify(edits)); } catch (e) {} }

let DATA = null, base = [], SOURCES = ["recco"], refId = null, sortKey = "idx", sortDir = 1;

// ------------------------------------------------------------ music helpers
function useFlat(k, m) { return (m === 1 && [1,3,5,8,10].includes(k)) || (m === 0 && [3,5,10].includes(k)); }
function keyName(k, m) { return k == null || m == null ? "" : (useFlat(k, m) ? NOTES_FLAT[k] : NOTES[k]) + (m === 1 ? " メジャー" : " マイナー"); }
function keyShort(k, m) { return k == null ? "–" : (useFlat(k, m) ? NOTES_FLAT[k] : NOTES[k]) + (m === 0 ? "m" : ""); }
function camelot(k, m) {
  if (k == null || m == null) return null;
  const majorTonic = m === 1 ? k : mod(k + 3, 12);
  const n = mod(majorTonic * 7 + 7, 12) + 1;
  return { n, l: m === 1 ? "B" : "A", s: n + (m === 1 ? "B" : "A") };
}
function camDist(a, b) { return Math.min(mod(a.n - b.n, 12), mod(b.n - a.n, 12)); }
function compatOf(cam, refCam) {
  const dist = camDist(cam, refCam);
  if (dist === 0 && cam.l === refCam.l) return { t: "同じキー", c: "good", rank: 0 };
  if (dist === 0) return { t: "平行調", c: "good", rank: 1 };
  if (dist === 1 && cam.l === refCam.l) return { t: "隣接 ±1", c: "good", rank: 2 };
  if (dist === 1) return { t: "斜め隣", c: "warn", rank: 3 };
  if (dist === 2) return { t: "±2", c: "warn", rank: 4 };
  return { t: "離れている (" + dist + ")", c: "far", rank: 4 + dist };
}
// v を ref のキーに合わせる移調量（長短が違えば平行調どうしで比較）
function keyShiftTo(v, ref) {
  const same = v.mode === ref.mode;
  const tonic = x => same ? x.key : (x.mode === 1 ? x.key : mod(x.key + 3, 12));
  return signed(tonic(ref) - tonic(v));
}
function fmtSigned(v, digits) {
  if (v == null || isNaN(v)) return "–";
  const s = v.toFixed(digits);
  if (Number(s) === 0) return (0).toFixed(digits);
  return (v > 0 ? "+" : "−") + Math.abs(v).toFixed(digits);
}
function fmtBpm(b) { return b ? Number(b).toFixed(b % 1 ? 1 : 0) : "–"; }
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])); }

// ------------------------------------------------------------ cross-check
function hasVal(v) { return v && !v.pending && (v.bpm || v.key != null); }
function bpmRel(a, b) {
  const r = a / b;
  if (Math.abs(r - 1) <= 0.03) return "same";
  if (Math.abs(r - 2) <= 0.06 || Math.abs(r - 0.5) <= 0.015) return "double";
  if (Math.abs(r - 1.5) <= 0.045 || Math.abs(r - 2/3) <= 0.02) return "triplet";
  return "diff";
}
function keyRel(a, b) {
  if (a.mode == null || b.mode == null) return a.key === b.key ? "same" : "diff";
  if (a.key === b.key && a.mode === b.mode) return "same";
  const ca = camelot(a.key, a.mode), cb = camelot(b.key, b.mode);
  if (ca.n === cb.n) return "relative";
  if (a.key === b.key) return "parallel";
  if (camDist(ca, cb) === 1 && ca.l === cb.l) return "near";
  return "diff";
}
const REL_TEXT = {
  bpm: { double: ["BPMが倍/半分", "warn"], triplet: ["BPMが3:2", "warn"], diff: ["BPM不一致", "far"] },
  key: { relative: ["平行調のずれ", "warn"], parallel: ["同主調のずれ", "warn"], near: ["キーが隣接（±1）", "warn"], diff: ["キー不一致", "far"] }
};
const REL_ORDER = { same: 0, near: 1, relative: 1, parallel: 1, double: 1, triplet: 1, diff: 2 };
function pickBy(list, rel) {
  // list は優先順（GetSongBPM が先頭）。他の2つ以上が一致して先頭だけ外れている場合のみ多数派を採用
  let best = null, bestScore = -1;
  list.forEach(c => {
    const score = list.filter(o => o !== c && rel(o, c) === "same").length;
    if (score > bestScore) { best = c; bestScore = score; }
  });
  if (!list.length) return null;
  const firstScore = list.filter(o => o !== list[0] && rel(o, list[0]) === "same").length;
  return bestScore >= 2 && firstScore < bestScore ? best : list[0];
}
function prepare(r) {
  const avail = SOURCES.map(s => ({ s, v: (r.src || {})[s] })).filter(o => hasVal(o.v));
  const pb = pickBy(avail.filter(o => o.v.bpm), (a, b) => bpmRel(a.v.bpm, b.v.bpm));
  const pk = pickBy(avail.filter(o => o.v.key != null), (a, b) => keyRel(a.v, b.v));
  r.bpm = pb ? pb.v.bpm : null; r.key = pk ? pk.v.key : null; r.mode = pk ? pk.v.mode : null;
  const reasons = [];
  if (avail.length === 0) { r.check = { t: "データなし", c: "none", rank: 5, reasons }; return; }
  if (avail.length === 1) { r.check = { t: "1件のみ", c: "single", rank: 4, reasons, only: avail[0].s }; return; }
  let wb = "same", wk = "same";
  for (let i = 0; i < avail.length; i++) for (let j = i + 1; j < avail.length; j++) {
    const a = avail[i].v, b = avail[j].v;
    if (a.bpm && b.bpm) { const x = bpmRel(a.bpm, b.bpm); if (REL_ORDER[x] > REL_ORDER[wb]) wb = x; }
    if (a.key != null && b.key != null) { const x = keyRel(a, b); if (REL_ORDER[x] > REL_ORDER[wk]) wk = x; }
  }
  if (wb !== "same") reasons.push(REL_TEXT.bpm[wb]);
  if (wk !== "same") reasons.push(REL_TEXT.key[wk]);
  const far = reasons.some(x => x[1] === "far");
  r.check = reasons.length ? { t: "", c: far ? "far" : "warn", rank: far ? 0 : 1, reasons } : { t: "一致", c: "good", rank: 3, reasons };
}
function eff(r) {
  const e = edits[r.id] || {};
  return { bpm: e.bpm !== undefined ? e.bpm : r.bpm, key: e.key !== undefined ? e.key : r.key,
           mode: e.mode !== undefined ? e.mode : r.mode, edited: !!edits[r.id] };
}
function needsCheck(r) { return !edits[r.id] && (r.check.c === "warn" || r.check.c === "far"); }
function checkChip(r) {
  const ck = r.check;
  if (edits[r.id]) return `<span class="chip manual">手動で決定</span>`;
  if (ck.c === "good") return `<span class="chip good">一致</span>`;
  if (ck.c === "none") return `<span class="chip none">データなし</span>`;
  if (ck.c === "single") return `<span class="chip none" title="照合できるデータが1つだけです">${SRC_LABEL[ck.only][1]}のみ</span>`;
  return `<div class="reasons">${ck.reasons.map(x => `<span class="chip ${x[1]}">${x[0]}</span>`).join("")}</div>`;
}
function songCell(r, extra) {
  const demo = r.id.startsWith("demo");
  const inner = `<b>${esc(r.title || "（曲名不明）")}</b><span>${esc(r.artist || "")}</span>${extra || ""}`;
  return demo ? inner : `<a href="https://open.spotify.com/track/${r.id}" target="_blank" rel="noopener">${inner}</a>`;
}

// ------------------------------------------------------------ data load
function setData(d) {
  DATA = d; base = d.rows || []; SOURCES = PRIORITY.filter(s => (d.sources || ["recco"]).includes(s));
  base.forEach(prepare);
  if (!refId || !base.find(r => r.id === refId)) refId = base[0] && base[0].id;
  $("result").hidden = false;
  $("steps").hidden = true;
  $("resTitle").textContent = d.title || "解析結果";
  const missing = base.filter(r => r.bpm == null).length;
  $("resMeta").textContent = `${base.length} 曲 ・ ${d.generated || ""} ・ 取得元: ${SOURCES.map((s, i) => SRC_LABEL[s][1] + (i === 0 && s === "gsbpm" ? "（標準）" : "")).join(" / ")}` +
    (missing ? ` ・ データなし ${missing} 曲` : "") + (d.demo ? " ・ サンプルデータです" : "");
  const sel = $("pFocus"), cur = sel.value;
  sel.innerHTML = `<option value="">すべての組み合わせ</option>` +
    base.filter(r => r.bpm || r.key != null).map(r => `<option value="${r.id}">${esc((r.title || r.id) + (r.artist ? " / " + r.artist : ""))}</option>`).join("");
  if (base.find(r => r.id === cur)) sel.value = cur;
  renderAll();
}
function renderAll() { renderPairs(); renderRef(); }

// ------------------------------------------------------------ mashup pairs
function computePair(ra, rb, opt) {
  const a = eff(ra), b = eff(rb);
  if (!a.bpm || !b.bpm || a.key == null || b.key == null || a.mode == null || b.mode == null) return null;
  let best = { val: b.bpm, tag: "" };
  if (opt.hd) for (const [val, tag] of [[b.bpm * 2, "×2"], [b.bpm / 2, "÷2"]])
    if (Math.abs(val - a.bpm) < Math.abs(best.val - a.bpm)) best = { val, tag };
  const pct = (best.val - a.bpm) / a.bpm * 100;
  if (Math.abs(pct) > opt.tol + 1e-9) return null;
  const ca = camelot(a.key, a.mode), cb = camelot(b.key, b.mode);
  const compat = compatOf(cb, ca);
  const shift = keyShiftTo(b, a);
  let keyCost, keyText, keyClass;
  if (compat.rank <= 1) { keyCost = 0; keyText = compat.rank === 0 ? "同じキー・移調不要" : "平行調・移調不要"; keyClass = "good"; }
  else if (compat.rank === 2) { keyCost = 0.5; keyText = "隣接キー・移調なしでも可"; keyClass = "good"; }
  else {
    if (Math.abs(shift) > opt.maxShift) return null;
    keyCost = Math.abs(shift);
    keyText = `Bを ${fmtSigned(shift, 0)} 半音`;
    keyClass = keyCost <= 2 ? "warn" : "far";
  }
  const score = Math.abs(pct) + keyCost * 2;
  return { ra, rb, a, b, ca, cb, pct, tag: best.tag, shift, keyCost, keyText, keyClass, score,
           check: needsCheck(ra) || needsCheck(rb), verified: ra.check.c === "good" && rb.check.c === "good" || (!!edits[ra.id] && !!edits[rb.id]) };
}
function renderPairs() {
  if (!DATA) return;
  const opt = { tol: Math.max(0, parseFloat($("pTol").value) || 0), maxShift: parseInt($("pShift").value, 10), hd: $("pHd").checked };
  const focus = $("pFocus").value, onlyVerified = $("pVerified").checked, sortBy = $("pSort").value;
  const okRow = r => !onlyVerified || r.check.c === "good" || !!edits[r.id];
  const list = [];
  if (focus) {
    const fr = base.find(r => r.id === focus);
    for (const r of base) if (r !== fr && okRow(r)) { const p = computePair(fr, r, opt); if (p) list.push(p); }
  } else {
    const rows = base.filter(okRow);
    for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
      // テンポの速い方を A にすると読みやすい
      const [x, y] = (eff(rows[i]).bpm || 0) >= (eff(rows[j]).bpm || 0) ? [rows[i], rows[j]] : [rows[j], rows[i]];
      const p = computePair(x, y, opt); if (p) list.push(p);
    }
  }
  const cmp = { score: (p, q) => p.score - q.score,
                bpm: (p, q) => Math.abs(p.pct) - Math.abs(q.pct) || p.keyCost - q.keyCost,
                key: (p, q) => p.keyCost - q.keyCost || Math.abs(p.pct) - Math.abs(q.pct) }[sortBy];
  list.sort(cmp);
  const shown = list.slice(0, 300);
  const usable = base.filter(r => { const v = eff(r); return v.bpm && v.key != null; }).length;
  $("pSummary").textContent = list.length
    ? `${list.length} 組の候補が見つかりました` + (list.length > shown.length ? `（上位 ${shown.length} 組を表示）` : "") +
      (focus ? "" : `　・BPMとキーがそろっている曲 ${usable} / ${base.length} 曲で計算`)
    : "条件に合う組み合わせがありません。BPM差や移調の範囲を広げてみてください。";
  const maxScore = opt.tol + opt.maxShift * 2 || 1;
  $("pairBody").innerHTML = shown.map((p, i) => {
    const vals = (v, c) => `<span class="vals">${fmtBpm(v.bpm)} BPM · ${keyShort(v.key, v.mode)}（${c.s}）</span>`;
    const pctTxt = Math.abs(p.pct) < 0.05 ? "そのまま" : `${fmtSigned(p.pct, 1)}%`;
    const tempo = `<div class="stack"><span class="chip ${Math.abs(p.pct) <= 3 ? "good" : (Math.abs(p.pct) <= 6 ? "warn" : "far")}">${pctTxt}</span>` +
      `<span class="small num">${p.tag ? `B${p.tag}で ` : ""}${fmtBpm(p.b.bpm)} → ${fmtBpm(p.a.bpm)}</span></div>`;
    const chk = p.check
      ? `<span class="chip warn" title="${esc([needsCheck(p.ra) ? "曲A" : "", needsCheck(p.rb) ? "曲B" : ""].filter(Boolean).join("・"))}の値がデータベース間でずれています">要確認</span>`
      : (p.verified ? `<span class="chip good">一致</span>` : `<span class="chip none" title="照合できるデータが1つだけの曲を含みます">照合なし</span>`);
    const pctScore = Math.max(4, Math.round(100 - p.score / maxScore * 100));
    return `<tr>
      <td class="r num">${i + 1}</td>
      <td class="song">${songCell(p.ra, vals(p.a, p.ca))}</td>
      <td class="pair-arrow" aria-hidden="true">×</td>
      <td class="song">${songCell(p.rb, vals(p.b, p.cb))}</td>
      <td>${tempo}</td>
      <td><span class="chip ${p.keyClass}">${p.keyText}</span></td>
      <td>${chk}</td>
      <td><span class="score" title="テンポ差と移調量から計算"><i><b style="width:${pctScore}%"></b></i><span class="num small">${pctScore}</span></span></td>
      <td><button type="button" data-compare="${p.ra.id}">Aを基準に比較</button></td>
    </tr>`;
  }).join("");
}

// ------------------------------------------------------------ reference table
function computeRef() {
  const refRow = base.find(r => r.id === refId) || base[0];
  const ref = refRow ? eff(refRow) : {};
  const refCam = camelot(ref.key, ref.mode);
  const tol = Math.max(0, parseFloat($("tol").value) || 0);
  const hd = $("hd").checked;
  return base.map((r, i) => {
    const v = eff(r);
    const o = { r, v, idx: i + 1, title: (r.title || r.id).toLowerCase(), bpm: v.bpm ?? Infinity,
      keyIdx: v.key == null ? 99 : v.mode * 12 + v.key, isRef: r.id === refRow?.id };
    o.checkRank = v.edited ? 6 : r.check.rank; o.needsCheck = needsCheck(r);
    const cam = camelot(v.key, v.mode);
    o.cam = cam; o.camelotSort = cam ? cam.n * 2 + (cam.l === "B" ? 1 : 0) : 999;
    if (v.bpm && ref.bpm) {
      let best = { val: v.bpm, tag: "" };
      if (hd) for (const [val, tag] of [[v.bpm * 2, "×2"], [v.bpm / 2, "÷2"]])
        if (Math.abs(val - ref.bpm) < Math.abs(best.val - ref.bpm)) best = { val, tag };
      o.bpmTag = best.tag; o.bpmDiff = best.val - ref.bpm; o.bpmPct = o.bpmDiff / ref.bpm * 100;
      o.bpmDiffAbs = Math.abs(o.bpmDiff); o.bpmPctAbs = Math.abs(o.bpmPct);
      o.bpmNear = o.bpmPctAbs <= tol + 1e-9;
      o.pitch = 12 * Math.log2(ref.bpm / best.val);
    } else { o.bpmDiffAbs = o.bpmPctAbs = Infinity; o.bpmNear = false; }
    if (cam && refCam) {
      o.keyShift = keyShiftTo(v, ref); o.relative = v.mode !== ref.mode; o.keyShiftAbs = Math.abs(o.keyShift);
      o.compat = compatOf(cam, refCam); o.keyGood = o.compat.c === "good"; o.compatRank = o.compat.rank;
      if (o.pitch != null) { o.after = mod(o.keyShift - o.pitch + 6, 12) - 6; o.afterAbs = Math.abs(o.after); }
      else o.afterAbs = Infinity;
    } else { o.keyShiftAbs = o.compatRank = o.afterAbs = Infinity; o.keyGood = false; }
    return o;
  });
}
function keyOptions(k, m) {
  let h = `<option value="">–</option>`;
  for (const mm of [1, 0]) for (let kk = 0; kk < 12; kk++)
    h += `<option value="${kk},${mm}"${k === kk && m === mm ? " selected" : ""}>${keyName(kk, mm)}（${camelot(kk, mm).s}）</option>`;
  return h;
}
function renderRef() {
  if (!DATA) return;
  const rows = computeRef();
  const refO = rows.find(o => o.isRef);
  const q = $("q").value.trim().toLowerCase(), show = $("show").value, tol = $("tol").value;
  if (refO) {
    $("refPanel").innerHTML = `<div class="label">基準曲</div>
      <p class="t">${esc(refO.r.title || "（曲名不明）")}</p><div class="a">${esc(refO.r.artist)}</div>
      <div class="facts"><div><span class="label">BPM</span><b>${refO.v.bpm ? Number(refO.v.bpm).toFixed(1) : "–"}</b></div>
      <div><span class="label">キー</span><b style="font-family:var(--font-body)">${refO.v.key != null ? keyName(refO.v.key, refO.v.mode) : "–"}</b></div>
      <div><span class="label">Camelot</span><b>${refO.cam ? refO.cam.s : "–"}</b></div></div>`;
  }
  const others = rows.filter(o => !o.isRef);
  $("sBpm").textContent = `${others.filter(o => o.bpmNear).length} / ${others.length}`;
  $("sBpmNote").textContent = `±${tol}% 以内`;
  $("sKey").textContent = `${others.filter(o => o.keyGood).length} / ${others.length}`;
  $("sBoth").textContent = `${others.filter(o => o.keyGood && o.bpmNear).length} 曲`;
  const nCheck = rows.filter(o => o.needsCheck).length;
  $("sCheck").textContent = `${nCheck} 曲`;
  $("sCheckNote").textContent = SOURCES.length > 1 ? "データベース間でずれ" : "照合先が1つだけです（設定で追加）";
  $("checkCount").hidden = !nCheck; $("checkCount").textContent = `要確認 ${nCheck}`;

  rows.sort((a, b) => {
    if (a.isRef !== b.isRef && sortKey !== "idx") return a.isRef ? -1 : 1;
    const x = a[sortKey], y = b[sortKey];
    if (x === y) return a.idx - b.idx;
    if (x === Infinity) return 1; if (y === Infinity) return -1;
    return (x > y ? 1 : -1) * sortDir;
  });
  document.querySelectorAll("#refTable th[data-sort]").forEach(th => {
    th.querySelector(".arrow")?.remove();
    if (th.dataset.sort === sortKey) th.insertAdjacentHTML("beforeend", `<span class="arrow">${sortDir > 0 ? "▲" : "▼"}</span>`);
  });
  $("tbody").innerHTML = rows.map(o => {
    const hay = (o.r.title + " " + o.r.artist).toLowerCase();
    const pass = (!q || hay.includes(q)) && (o.isRef || show === "all" || (show === "bpm" && o.bpmNear) ||
      (show === "key" && o.keyGood) || (show === "both" && o.bpmNear && o.keyGood) || (show === "check" && o.needsCheck));
    if (!pass) return "";
    const v = o.v, id = o.r.id;
    const srcCell = `<div class="srcs">` + SOURCES.map(s => {
      const sv = (o.r.src || {})[s], lab = SRC_LABEL[s];
      if (sv && sv.pending) return `<span class="src empty" title="${lab[1]}で解析中です。しばらくして取り直してください"><span class="n">${lab[0]}</span>解析中</span>`;
      if (!hasVal(sv)) return `<span class="src empty" title="${lab[1]}に見つかりませんでした"><span class="n">${lab[0]}</span>なし</span>`;
      const on = (sv.bpm ? Math.abs((v.bpm || 0) - sv.bpm) < 0.051 : !v.bpm) && sv.key === v.key && sv.mode === v.mode;
      const tip = [lab[1] + "の値を採用", sv.matched ? "一致した曲: " + sv.matched : "",
        sv.match === "title-only" ? "※曲名のみで照合" : (sv.match === "fuzzy" ? "※曲名が完全一致ではありません" : ""),
        sv.key_conf != null ? "キー信頼度 " + sv.key_conf : ""].filter(Boolean).join("\n");
      return `<button type="button" class="src${on ? " on" : ""}" data-adopt="${s}" data-id="${id}" title="${esc(tip)}"><span class="n">${lab[0]}</span>${fmtBpm(sv.bpm)} · ${keyShort(sv.key, sv.mode)}${sv.match && sv.match !== "exact" ? " ?" : ""}</button>`;
    }).join("") + `</div>`;
    const bpmCell = o.isRef || o.bpmDiff == null ? "–" :
      `<span class="${Math.abs(o.bpmDiff) < 0.05 ? "zero" : ""}">${fmtSigned(o.bpmDiff, 1)}</span>${o.bpmTag ? `<span class="tag">${o.bpmTag}</span>` : ""}`;
    const pctCell = o.isRef || o.bpmPct == null ? "–" :
      `<span class="chip ${o.bpmNear ? "good" : (o.bpmPctAbs <= 2 * tol ? "warn" : "far")}">${fmtSigned(o.bpmPct, 1)}%</span>`;
    const keyCell = o.isRef || o.keyShift == null ? "–" :
      `<span class="${o.keyShift === 0 ? "zero" : ""}">${fmtSigned(o.keyShift, 0)}</span>${o.relative ? `<span class="tag">平行調基準</span>` : ""}`;
    const compatCell = o.isRef ? `<span class="badge-ref">基準</span>` :
      (o.compat ? `<span class="chip ${o.compat.c}">${o.compat.t}</span>` : `<span class="chip none">データなし</span>`);
    return `<tr class="${o.isRef ? "is-ref" : ""}">
      <td class="r num">${o.idx}</td>
      <td class="song">${songCell(o.r)}</td>
      <td class="r"><input class="bpm${edits[id]?.bpm !== undefined ? " edited" : ""}" id="bpm-${id}" data-id="${id}" type="number" step="0.1" min="0" value="${v.bpm ?? ""}" placeholder="–" aria-label="BPM"></td>
      <td><select class="key${edits[id]?.key !== undefined ? " edited" : ""}" id="key-${id}" data-id="${id}" aria-label="キー">${keyOptions(v.key, v.mode)}</select></td>
      <td>${checkChip(o.r)}</td>
      <td>${srcCell}</td>
      <td class="num">${o.cam ? o.cam.s : "–"}</td>
      <td class="r num">${bpmCell}</td>
      <td class="r num">${pctCell}</td>
      <td class="r num">${keyCell}</td>
      <td>${compatCell}</td>
      <td class="r num">${o.isRef || o.after == null ? "–" : fmtSigned(o.after, 1)}</td>
      <td>${o.isRef ? "" : `<button type="button" data-ref="${id}">基準にする</button>`}</td>
    </tr>`;
  }).join("");
  window.__rows = rows;
}

// ------------------------------------------------------------ ui events
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 1800);
}
function selectTab(which) {
  const pairs = which === "pairs";
  $("tabPairs").setAttribute("aria-selected", pairs); $("tabRef").setAttribute("aria-selected", !pairs);
  $("panelPairs").hidden = !pairs; $("panelRef").hidden = pairs;
}
$("tabPairs").addEventListener("click", () => selectTab("pairs"));
$("tabRef").addEventListener("click", () => selectTab("ref"));
$("pairBody").addEventListener("click", e => {
  const b = e.target.closest("button[data-compare]");
  if (b) { refId = b.dataset.compare; renderRef(); selectTab("ref"); window.scrollTo({ top: $("result").offsetTop - 60, behavior: "smooth" }); }
});
["pFocus", "pTol", "pShift", "pHd", "pVerified", "pSort"].forEach(id => $(id).addEventListener("input", renderPairs));
["q", "tol", "hd", "show"].forEach(id => $(id).addEventListener("input", renderRef));
document.querySelectorAll("#refTable th[data-sort]").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = 1; }
  renderRef();
}));
$("tbody").addEventListener("click", e => {
  const ad = e.target.closest("button[data-adopt]");
  if (ad) {
    const r = base.find(x => x.id === ad.dataset.id), sv = r.src[ad.dataset.adopt], e2 = {};
    if (sv.bpm && sv.bpm !== r.bpm) e2.bpm = sv.bpm;
    if (sv.key != null && (sv.key !== r.key || sv.mode !== r.mode)) { e2.key = sv.key; e2.mode = sv.mode; }
    if (Object.keys(e2).length) edits[r.id] = e2; else delete edits[r.id];
    saveEdits(); renderAll(); toast(SRC_LABEL[ad.dataset.adopt][1] + " の値を採用しました");
    return;
  }
  const b = e.target.closest("button[data-ref]");
  if (b) { refId = b.dataset.ref; renderRef(); window.scrollTo({ top: $("result").offsetTop - 60, behavior: "smooth" }); }
});
$("tbody").addEventListener("change", e => {
  const id = e.target.dataset.id; if (!id) return;
  const orig = base.find(r => r.id === id);
  edits[id] = edits[id] || {};
  if (e.target.classList.contains("bpm")) {
    const val = parseFloat(e.target.value), nv = isNaN(val) || val <= 0 ? null : val;
    if (nv === orig.bpm) delete edits[id].bpm; else edits[id].bpm = nv;
  } else {
    const [k, m] = e.target.value ? e.target.value.split(",").map(Number) : [null, null];
    if (k === orig.key && m === orig.mode) { delete edits[id].key; delete edits[id].mode; }
    else { edits[id].key = k; edits[id].mode = m; }
  }
  if (!Object.keys(edits[id]).length) delete edits[id];
  saveEdits(); renderAll();
});
$("resetEdits").addEventListener("click", () => {
  const ids = base.map(r => r.id).filter(id => edits[id]);
  if (!ids.length) { toast("手入力した値はありません"); return; }
  ids.forEach(id => delete edits[id]); saveEdits(); renderAll(); toast("このプレイリストの手入力をリセットしました");
});
$("csv").addEventListener("click", () => {
  const head = ["#","曲","アーティスト","Spotify","BPM","キー","Camelot","照合",...SOURCES.map(s => SRC_LABEL[s][1]),"BPM差","BPM差%","換算","キー差(半音)","キーの相性","基準曲"];
  const lines = [head];
  for (const o of window.__rows) lines.push([o.idx, o.r.title, o.r.artist, "https://open.spotify.com/track/" + o.r.id,
    o.v.bpm ?? "", o.v.key != null ? keyName(o.v.key, o.v.mode) : "", o.cam ? o.cam.s : "",
    o.v.edited ? "手動" : (o.r.check.reasons.length ? o.r.check.reasons.map(x => x[0]).join(" / ") : o.r.check.t),
    ...SOURCES.map(s => { const sv = (o.r.src || {})[s]; return hasVal(sv) ? `${sv.bpm ?? "–"} ${keyShort(sv.key, sv.mode)}` : ""; }),
    o.bpmDiff != null ? o.bpmDiff.toFixed(2) : "", o.bpmPct != null ? o.bpmPct.toFixed(2) : "", o.bpmTag || "",
    o.keyShift ?? "", o.compat ? o.compat.t : "", o.isRef ? "基準" : ""]);
  const csv = "﻿" + lines.map(l => l.map(c => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = (DATA.title || "key_bpm").replace(/[\\/:*?"<>|]/g, "_") + ".csv"; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
});

// ------------------------------------------------------------ server calls
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `エラー (${res.status})`);
  return data;
}
function showError(msg) { $("error").hidden = !msg; $("error").textContent = msg || ""; }
async function refreshHistory(selectId) {
  try {
    const items = await api("/api/history");
    $("history").innerHTML = items.length
      ? `<option value="">開く…</option>` + items.map(h => `<option value="${h.id}">${esc(h.title)}（${h.count}曲・${esc(h.generated)}）</option>`).join("")
      : `<option value="">履歴はまだありません</option>`;
    if (selectId) $("history").value = selectId;
    return items;
  } catch (e) { return []; }
}
async function refreshSettings() {
  try {
    const s = await api("/api/settings");
    $("gsSet").hidden = !s.getsongbpm; $("ssSet").hidden = !s.soundstat;
    const on = ["ReccoBeats"].concat(s.getsongbpm ? ["GetSongBPM"] : [], s.soundstat ? ["SoundStat"] : []);
    $("srcNote").innerHTML = s.getsongbpm ? `取得元: ${["GetSongBPM（標準）", "ReccoBeats"].concat(s.soundstat ? ["SoundStat"] : []).join(" / ")}` : "";
    $("keyNotice").hidden = !!s.getsongbpm;
    $("srcNoteBtn")?.addEventListener("click", openSettings);
  } catch (e) {}
}
$("history").addEventListener("change", async e => {
  if (!e.target.value) return;
  try { setData(await api("/api/history/" + e.target.value)); showError(""); } catch (err) { showError(err.message); }
});
$("demo").addEventListener("click", async () => {
  try { setData(await api("/api/history/demo")); } catch (err) { showError(err.message); }
});
$("analyze").addEventListener("click", async () => {
  const text = $("input").value.trim();
  if (!text) { showError("Spotify のプレイリストURLを貼り付けてください。"); $("input").focus(); return; }
  showError(""); $("log").innerHTML = ""; $("analyze").disabled = true; $("progress").hidden = false;
  try {
    const { job } = await api("/api/analyze", { text, refresh: $("refresh").checked });
    while (true) {
      await new Promise(r => setTimeout(r, 700));
      const j = await api("/api/job/" + job);
      const pb = $("pbar");
      pb.classList.toggle("indet", !j.total);
      pb.querySelector("i").style.width = j.total ? Math.round(j.done / j.total * 100) + "%" : "";
      $("stage").textContent = j.total ? `${j.stage}　${j.done} / ${j.total} 曲` : (j.stage || "");
      $("log").innerHTML = (j.log || []).map(l => `<div>${esc(l)}</div>`).join("");
      if (j.status === "done") { setData(j.result); refreshHistory(j.result.id); selectTab("pairs"); toast("解析が終わりました"); break; }
      if (j.status === "error") { showError(j.error); break; }
    }
  } catch (err) {
    showError(err.message === "Failed to fetch" ? "ツールが停止しています。起動ファイルをもう一度ダブルクリックしてください。" : err.message);
  } finally { $("analyze").disabled = false; $("progress").hidden = true; }
});
$("input").addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("analyze").click(); });

function openSettings() { $("gsKey").value = ""; $("ssKey").value = ""; $("settings").showModal(); }
$("openSettings").addEventListener("click", openSettings);
$("keyNoticeBtn").addEventListener("click", openSettings);
$("cancelSettings").addEventListener("click", () => $("settings").close());
$("showKeys").addEventListener("change", e => { $("gsKey").type = $("ssKey").type = e.target.checked ? "text" : "password"; });
$("saveSettings").addEventListener("click", async () => {
  const body = {};
  if ($("gsKey").value.trim()) body.getsongbpm_api_key = $("gsKey").value.trim();
  if ($("ssKey").value.trim()) body.soundstat_api_key = $("ssKey").value.trim();
  try { await api("/api/settings", body); $("settings").close(); refreshSettings(); toast(Object.keys(body).length ? "保存しました。次の解析から照合に使います" : "変更はありません"); }
  catch (err) { toast(err.message); }
});
$("clearKeys").addEventListener("click", async () => {
  try { await api("/api/settings", { getsongbpm_api_key: "", soundstat_api_key: "" }); refreshSettings(); toast("APIキーを削除しました"); } catch (err) { toast(err.message); }
});
$("quit").addEventListener("click", async () => {
  try { await api("/api/quit", {}); } catch (e) {}
  document.body.innerHTML = `<main class="wrap"><div class="card"><h1>終了しました</h1><p class="sub">このタブは閉じて大丈夫です。もう一度使うときは、起動ファイルをダブルクリックしてください。</p></div></main>`;
});

// 起動時: 設定と履歴を読み込み、前回の解析結果を開く
(async () => {
  await refreshSettings();
  const items = await refreshHistory();
  if (items.length) {
    try { setData(await api("/api/history/" + items[0].id)); $("history").value = items[0].id; } catch (e) {}
  }
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
