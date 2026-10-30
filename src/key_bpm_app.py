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


APP_HTML = None  # build 時に app.html の内容が入ります

if __name__ == "__main__":
    main()
